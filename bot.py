# -*- coding: utf-8 -*-

import os
import re
import sqlite3
import asyncio
import logging
import time

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

OWNER_ID = 8552447077
CHANNEL = "@zobxt"

DB_FILE = "bot.db"

MIN_BET = 0.1
REFERRAL_REWARD = 0.05

# هر دو بازیکن 0.1 می‌دهند
# برنده 0.19 می‌گیرد
PAYOUT = 1.90

GAME_TIMEOUT = 180

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

log = logging.getLogger("BET_BT")

DB_LOCK = asyncio.Lock()


# =========================================================
# DATABASE
# =========================================================

def connect():
    con = sqlite3.connect(DB_FILE, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():

    con = connect()
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            referrer INTEGER DEFAULT NULL,
            ref_paid INTEGER DEFAULT 0,
            created_at REAL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT NOT NULL,
            stake REAL NOT NULL,
            mode TEXT DEFAULT 'bot',
            creator_roll INTEGER DEFAULT NULL,
            opponent_roll INTEGER DEFAULT NULL,
            bot_roll INTEGER DEFAULT NULL,
            status TEXT DEFAULT 'waiting_creator',
            settled INTEGER DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            created_at REAL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("""
        INSERT OR IGNORE INTO settings(key,value)
        VALUES('enabled','1')
    """)

    con.commit()
    con.close()


# =========================================================
# HELPERS
# =========================================================

def normalize(text):

    return text.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


def fmt(number):

    return f"{float(number):.8f}".rstrip("0").rstrip(".")


def user_name(user):

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def ensure_user(user_id, first_name="", username="", referrer=None):

    con = connect()

    row = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    if not row:

        con.execute(
            """
            INSERT INTO users
            (user_id,first_name,username,referrer,created_at)
            VALUES(?,?,?,?,?)
            """,
            (
                user_id,
                first_name or "",
                username or "",
                referrer,
                time.time()
            )
        )

    else:

        con.execute(
            """
            UPDATE users
            SET first_name=?,username=?
            WHERE user_id=?
            """,
            (
                first_name or "",
                username or "",
                user_id
            )
        )

    con.commit()
    con.close()


def get_balance(user_id):

    con = connect()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    if not row:
        return 0.0

    return float(row["balance"])


def change_balance(user_id, amount, kind):

    con = connect()

    con.execute(
        """
        UPDATE users
        SET balance=ROUND(balance+?,8)
        WHERE user_id=?
        """,
        (amount, user_id)
    )

    con.execute(
        """
        INSERT INTO transactions
        (user_id,amount,kind,created_at)
        VALUES(?,?,?,?)
        """,
        (user_id, amount, kind, time.time())
    )

    con.commit()
    con.close()


def is_admin(user_id):

    if user_id == OWNER_ID:
        return True

    con = connect()

    row = con.execute(
        "SELECT user_id FROM admins WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    return bool(row)


def setting(key, default=""):

    con = connect()

    row = con.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,)
    ).fetchone()

    con.close()

    return row["value"] if row else default


def set_setting(key, value):

    con = connect()

    con.execute(
        """
        INSERT INTO settings(key,value)
        VALUES(?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, str(value))
    )

    con.commit()
    con.close()


# =========================================================
# CHANNEL JOIN
# =========================================================

async def check_join(bot, user_id):

    if user_id == OWNER_ID:
        return True

    try:

        member = await bot.get_chat_member(
            CHANNEL,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception:

        return False


async def require_join(update, context):

    user = update.effective_user

    if user.id == OWNER_ID:
        return True

    if await check_join(context.bot, user.id):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 عضویت در کانال",
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

    await update.effective_message.reply_text(
        "🔒 برای استفاده از ربات ابتدا در کانال عضو شوید.",
        reply_markup=keyboard
    )

    return False


# =========================================================
# GAME PARSER
# =========================================================

GAME_WORDS = {

    "تاس": "dice",
    "dice": "dice",

    "بولینگ": "bowling",
    "bowling": "bowling",

    "دارت": "darts",
    "darts": "darts",

    "بسکتبال": "basketball",
    "basketball": "basketball",
}


GAME_EMOJI = {

    "dice": "🎲",
    "bowling": "🎳",
    "darts": "🎯",
    "basketball": "🏀",
}


EMOJI_GAME = {

    "🎲": "dice",
    "🎳": "bowling",
    "🎯": "darts",
    "🏀": "basketball",
}


def parse_game(text):

    text = normalize(text.strip().lower())

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    for word, game_type in GAME_WORDS.items():

        # 1 تاس 0.1
        pattern = rf"^1\s+{re.escape(word)}\s+([0-9]+(?:\.[0-9]+)?)$"

        match = re.match(pattern, text)

        if match:

            return (
                game_type,
                float(match.group(1))
            )

        # تاس 0.1
        pattern = rf"^{re.escape(word)}\s+([0-9]+(?:\.[0-9]+)?)$"

        match = re.match(pattern, text)

        if match:

            return (
                game_type,
                float(match.group(1))
            )

    return None


# =========================================================
# TRANSFER PARSER
# =========================================================

def parse_transfer(text):

    text = normalize(text.strip())

    match = re.match(
        r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)$",
        text,
        re.I
    )

    if not match:
        return None

    return float(match.group(1))


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(
    update,
    context,
    game_type,
    amount
):

    user = update.effective_user
    chat = update.effective_chat

    if setting("enabled", "1") != "1":

        if not is_admin(user.id):

            await update.message.reply_text(
                "🚫 ربات فعلاً خاموش است."
            )

            return

    if amount < MIN_BET:

        await update.message.reply_text(
            f"❌ حداقل مبلغ بازی {fmt(MIN_BET)} است."
        )

        return

    # =====================================================
    # LOCK BALANCE
    # =====================================================

    async with DB_LOCK:

        con = connect()
        cur = con.cursor()

        row = cur.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user.id,)
        ).fetchone()

        if not row:

            con.close()

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

            return

        balance = float(row["balance"])

        if balance < amount:

            con.close()

            await update.message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        # فقط همین‌جا کسر می‌شود
        cur.execute(
            """
            UPDATE users
            SET balance=ROUND(balance-?,8)
            WHERE user_id=?
            """,
            (
                amount,
                user.id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions
            (user_id,amount,kind,created_at)
            VALUES(?,?,?,?)
            """,
            (
                user.id,
                -amount,
                "game_stake",
                time.time()
            )
        )

        cur.execute(
            """
            INSERT INTO games
            (
                chat_id,
                creator_id,
                game_type,
                stake,
                mode,
                status,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                chat.id,
                user.id,
                game_type,
                amount,
                "bot",
                "waiting_creator",
                time.time()
            )
        )

        game_id = cur.lastrowid

        con.commit()
        con.close()

    emoji = GAME_EMOJI[game_type]

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

    message = await update.message.reply_text(

        f"{emoji} بازی #{game_id} ساخته شد.\n\n"

        f"👤 سازنده: {user_name(user)}\n"
        f"💰 مبلغ: {fmt(amount)}\n\n"

        "👇 حالت بازی را انتخاب کنید:",

        reply_markup=keyboard
    )

    con = connect()

    con.execute(
        """
        UPDATE games
        SET message_id=?
        WHERE id=?
        """,
        (
            message.message_id,
            game_id
        )
    )

    con.commit()
    con.close()


# =========================================================
# REFUND
# =========================================================

async def refund_game(game_id, user_id):

    async with DB_LOCK:

        con = connect()
        cur = con.cursor()

        row = cur.execute(
            """
            SELECT *
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not row or row["settled"]:

            con.close()

            return False

        cur.execute(
            """
            UPDATE games
            SET settled=1,
                status='refunded'
            WHERE id=?
            """,
            (game_id,)
        )

        cur.execute(
            """
            UPDATE users
            SET balance=ROUND(balance+?,8)
            WHERE user_id=?
            """,
            (
                row["stake"],
                user_id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions
            (user_id,amount,kind,created_at)
            VALUES(?,?,?,?)
            """,
            (
                user_id,
                row["stake"],
                "game_refund",
                time.time()
            )
        )

        con.commit()
        con.close()

        return True


async def refund_both(game_id):

    async with DB_LOCK:

        con = connect()
        cur = con.cursor()

        row = cur.execute(
            """
            SELECT *
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not row or row["settled"]:

            con.close()

            return False

        cur.execute(
            """
            UPDATE games
            SET settled=1,
                status='refunded'
            WHERE id=?
            """,
            (game_id,)
        )

        for uid in (
            row["creator_id"],
            row["opponent_id"]
        ):

            if uid:

                cur.execute(
                    """
                    UPDATE users
                    SET balance=ROUND(balance+?,8)
                    WHERE user_id=?
                    """,
                    (
                        row["stake"],
                        uid
                    )
                )

                cur.execute(
                    """
                    INSERT INTO transactions
                    (user_id,amount,kind,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (
                        uid,
                        row["stake"],
                        "game_refund",
                        time.time()
                    )
                )

        con.commit()
        con.close()

        return True


# =========================================================
# SETTLE
# =========================================================

async def settle_game(game_id, winner_id):

    async with DB_LOCK:

        con = connect()
        cur = con.cursor()

        row = cur.execute(
            """
            SELECT *
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not row or row["settled"]:

            con.close()

            return False

        payout = round(
            float(row["stake"]) * PAYOUT,
            8
        )

        cur.execute(
            """
            UPDATE games
            SET settled=1,
                status='settled'
            WHERE id=?
            """,
            (game_id,)
        )

        cur.execute(
            """
            UPDATE users
            SET balance=ROUND(balance+?,8)
            WHERE user_id=?
            """,
            (
                payout,
                winner_id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions
            (user_id,amount,kind,created_at)
            VALUES(?,?,?,?)
            """,
            (
                winner_id,
                payout,
                "game_win",
                time.time()
            )
        )

        con.commit()
        con.close()

        return True


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    referrer = None

    if context.args:

        try:

            referrer = int(context.args[0])

            if referrer == user.id:
                referrer = None

        except Exception:

            referrer = None

    ensure_user(
        user.id,
        user.first_name,
        user.username,
        referrer
    )

    if not await require_join(update, context):
        return

    # =====================================================
    # REFERRAL
    # =====================================================

    if referrer:

        async with DB_LOCK:

            con = connect()

            row = con.execute(
                """
                SELECT referrer,ref_paid
                FROM users
                WHERE user_id=?
                """,
                (user.id,)
            ).fetchone()

            if (
                row
                and row["referrer"] == referrer
                and not row["ref_paid"]
            ):

                con.execute(
                    """
                    UPDATE users
                    SET balance=ROUND(balance+?,8),
                        ref_paid=1
                    WHERE user_id=?
                    """,
                    (
                        REFERRAL_REWARD,
                        referrer
                    )
                )

                con.execute(
                    """
                    INSERT INTO transactions
                    (user_id,amount,kind,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (
                        referrer,
                        REFERRAL_REWARD,
                        "referral",
                        time.time()
                    )
                )

                con.commit()

            con.close()

    # =====================================================
    # PRIVATE
    # =====================================================

    if update.effective_chat.type == ChatType.PRIVATE:

        keyboard = InlineKeyboardMarkup([

            [
                InlineKeyboardButton(
                    "💰 موجودی",
                    callback_data="balance"
                )
            ],

            [
                InlineKeyboardButton(
                    "👥 زیرمجموعه",
                    callback_data="ref"
                )
            ],

            [
                InlineKeyboardButton(
                    "📖 راهنما",
                    callback_data="help"
                )
            ]

        ])

        await update.message.reply_text(

            "🎮 BET_BT آماده است.\n\n"
            "برای بازی وارد گپ شوید و بنویس:\n\n"
            "🎲 1 تاس 0.1\n"
            "🎳 1 بولینگ 0.1\n"
            "🎯 1 دارت 0.1\n"
            "🏀 1 بسکتبال 0.1",

            reply_markup=keyboard
        )

        return

    # =====================================================
    # GROUP
    # =====================================================

    await update.message.reply_text(

        "🎮 BET_BT آماده است.\n\n"

        "🎲 1 تاس 0.1\n"
        "🎳 1 بولینگ 0.1\n"
        "🎯 1 دارت 0.1\n"
        "🏀 1 بسکتبال 0.1\n\n"

        "🤖 بازی با ربات:\n"
        "اول خود کاربر رول می‌کند، بعد ربات.\n\n"

        "👥 بازی با دوستان:\n"
        "اول سازنده خودش رول می‌کند، بعد حریف.\n\n"

        "💰 موجودی\n"
        "💸 انتقال با Reply: انتقال 0.1"
    )


# =========================================================
# BALANCE
# =========================================================

async def show_balance(update, context):

    user = update.effective_user

    ensure_user(
        user.id,
        user.first_name,
        user.username
    )

    if not await require_join(update, context):
        return

    await update.effective_message.reply_text(

        f"💰 موجودی شما:\n"
        f"{fmt(get_balance(user.id))}"
    )


# =========================================================
# TRANSFER
# =========================================================

async def do_transfer(update, context, amount):

    message = update.message
    sender = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "❌ باید روی پیام کاربر Reply بزنی.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    receiver = message.reply_to_message.from_user

    if receiver.id == sender.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if receiver.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    async with DB_LOCK:

        con = connect()
        cur = con.cursor()

        sender_row = cur.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (sender.id,)
        ).fetchone()

        if (
            not sender_row
            or float(sender_row["balance"]) < amount
        ):

            con.close()

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        # اطمینان از وجود گیرنده
        cur.execute(
            """
            INSERT OR IGNORE INTO users
            (user_id,first_name,username,created_at)
            VALUES(?,?,?,?)
            """,
            (
                receiver.id,
                receiver.first_name or "",
                receiver.username or "",
                time.time()
            )
        )

        cur.execute(
            """
            UPDATE users
            SET balance=ROUND(balance-?,8)
            WHERE user_id=?
            AND balance>=?
            """,
            (
                amount,
                sender.id,
                amount
            )
        )

        if cur.rowcount != 1:

            con.rollback()
            con.close()

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

        cur.execute(
            """
            UPDATE users
            SET balance=ROUND(balance+?,8)
            WHERE user_id=?
            """,
            (
                amount,
                receiver.id
            )
        )

        cur.execute(
            """
            INSERT INTO transactions
            (user_id,amount,kind,created_at)
            VALUES(?,?,?,?)
            """,
            (
                sender.id,
                -amount,
                "transfer_out",
                time.time()
            )
        )

        cur.execute(
            """
            INSERT INTO transactions
            (user_id,amount,kind,created_at)
            VALUES(?,?,?,?)
            """,
            (
                receiver.id,
                amount,
                "transfer_in",
                time.time()
            )
        )

        con.commit()
        con.close()

    await message.reply_text(

        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {user_name(receiver)}\n"
        f"💰 مبلغ: {fmt(amount)}"
    )


# =========================================================
# USER DICE / BOWLING / DART / BASKETBALL
# =========================================================

async def handle_roll(update, context):

    message = update.message
    user = update.effective_user

    if not message or not message.dice:
        return

    emoji = message.dice.emoji

    game_type = EMOJI_GAME.get(emoji)

    if not game_type:
        return

    con = connect()

    game = con.execute(
        """
        SELECT *
        FROM games
        WHERE chat_id=?
        AND game_type=?
        AND settled=0
        AND (
            (
                status='waiting_creator'
                AND creator_id=?
            )
            OR
            (
                status='waiting_opponent'
                AND opponent_id=?
            )
        )
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            message.chat_id,
            game_type,
            user.id,
            user.id
        )
    ).fetchone()

    con.close()

    if not game:
        return

    # =====================================================
    # TIMEOUT
    # =====================================================

    if time.time() - float(game["created_at"]) > GAME_TIMEOUT:

        if game["mode"] == "friend" and game["opponent_id"]:

            await refund_both(
                game["id"]
            )

        else:

            await refund_game(
                game["id"],
                game["creator_id"]
            )

        await message.reply_text(
            "⏰ بازی منقضی شد و مبلغ برگشت داده شد."
        )

        return

    value = message.dice.value

    # =====================================================
    # CREATOR ROLL
    # =====================================================

    if (
        game["status"] == "waiting_creator"
        and user.id == game["creator_id"]
    ):

        if game["mode"] == "bot":

            new_status = "waiting_bot"

        else:

            new_status = "waiting_opponent"

        con = connect()

        con.execute(
            """
            UPDATE games
            SET creator_roll=?,
                status=?
            WHERE id=?
            AND settled=0
            """,
            (
                value,
                new_status,
                game["id"]
            )
        )

        con.commit()
        con.close()

        if game["mode"] == "friend":

            await message.reply_text(

                f"🎲 {user_name(user)} رول کرد: {value}\n\n"
                "👥 حالا حریف باید خودش رول کند."
            )

            return

        # =================================================
        # BOT GAME
        # =================================================

        await message.reply_text(

            f"🎲 {user_name(user)} رول کرد: {value}\n\n"
            "🤖 حالا ربات رول می‌کند..."
        )

        try:

            bot_message = await context.bot.send_dice(
                chat_id=message.chat_id,
                emoji=emoji
            )

            bot_value = bot_message.dice.value

            con = connect()

            con.execute(
                """
                UPDATE games
                SET bot_roll=?,
                    status='settling'
                WHERE id=?
                AND settled=0
                """,
                (
                    bot_value,
                    game["id"]
                )
            )

            con.commit()
            con.close()

            # =================================================
            # DRAW
            # =================================================

            if value == bot_value:

                await refund_game(
                    game["id"],
                    game["creator_id"]
                )

                await context.bot.send_message(

                    chat_id=message.chat_id,

                    text=(
                        f"🎮 نتیجه بازی #{game['id']}\n\n"
                        f"👤 {user_name(user)}: {value}\n"
                        f"🤖 ربات: {bot_value}\n\n"
                        "🤝 مساوی شد!\n"
                        f"💰 مبلغ {fmt(game['stake'])} برگشت داده شد."
                    )
                )

                return

            # =================================================
            # USER WIN
            # =================================================

            if value > bot_value:

                await settle_game(
                    game["id"],
                    user.id
                )

                await context.bot.send_message(

                    chat_id=message.chat_id,

                    text=(
                        f"🎮 نتیجه بازی #{game['id']}\n\n"
                        f"👤 {user_name(user)}: {value}\n"
                        f"🤖 ربات: {bot_value}\n\n"
                        f"🏆 برنده: {user_name(user)}\n"
                        f"💰 جایزه: "
                        f"{fmt(game['stake'] * PAYOUT)}"
                    )
                )

                return

            # =================================================
            # BOT WIN
            # =================================================

            con = connect()

            con.execute(
                """
                UPDATE games
                SET settled=1,
                    status='settled'
                WHERE id=?
                AND settled=0
                """,
                (game["id"],)
            )

            con.commit()
            con.close()

            await context.bot.send_message(

                chat_id=message.chat_id,

                text=(
                    f"🎮 نتیجه بازی #{game['id']}\n\n"
                    f"👤 {user_name(user)}: {value}\n"
                    f"🤖 ربات: {bot_value}\n\n"
                    "🏆 برنده: 🤖 ربات"
                )
            )

        except Exception as error:

            log.exception(
                "Bot game error: %s",
                error
            )

            await refund_game(
                game["id"],
                game["creator_id"]
            )

            await message.reply_text(
                "🛡️ خطا در اجرای بازی.\n"
                "💰 مبلغ شما برگشت داده شد."
            )

        return

    # =====================================================
    # FRIEND OPPONENT ROLL
    # =====================================================

    if (
        game["mode"] == "friend"
        and game["status"] == "waiting_opponent"
        and user.id == game["opponent_id"]
    ):

        con = connect()

        con.execute(
            """
            UPDATE games
            SET opponent_roll=?,
                status='settling'
            WHERE id=?
            AND settled=0
            """,
            (
                value,
                game["id"]
            )
        )

        con.commit()
        con.close()

        creator_roll = game["creator_roll"]
        opponent_roll = value

        # =================================================
        # DRAW
        # =================================================

        if creator_roll == opponent_roll:

            await refund_both(
                game["id"]
            )

            await context.bot.send_message(

                chat_id=message.chat_id,

                text=(
                    f"🎮 نتیجه بازی #{game['id']}\n\n"
                    f"👤 سازنده: {creator_roll}\n"
                    f"👤 {user_name(user)}: {opponent_roll}\n\n"
                    "🤝 مساوی شد!\n"
                    "💰 مبالغ برگشت داده شد."
                )
            )

            return

        # =================================================
        # CREATOR WIN
        # =================================================

        if creator_roll > opponent_roll:

            winner = game["creator_id"]
            winner_text = "سازنده"

        else:

            winner = game["opponent_id"]
            winner_text = user_name(user)

        await settle_game(
            game["id"],
            winner
        )

        await context.bot.send_message(

            chat_id=message.chat_id,

            text=(
                f"🎮 نتیجه بازی #{game['id']}\n\n"
                f"👤 سازنده: {creator_roll}\n"
                f"👤 {user_name(user)}: {opponent_roll}\n\n"
                f"🏆 برنده: {winner_text}\n"
                f"💰 جایزه: "
                f"{fmt(game['stake'] * PAYOUT)}"
            )
        )


# =========================================================
# CALLBACKS
# =========================================================

async def callbacks(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user
    data = query.data

    # =====================================================
    # JOIN
    # =====================================================

    if data == "check_join":

        if await check_join(
            context.bot,
            user.id
        ):

            await query.message.reply_text(
                "✅ عضویت شما تأیید شد."
            )

        else:

            await query.message.reply_text(
                "❌ هنوز عضو کانال نیستید."
            )

        return

    # =====================================================
    # BALANCE
    # =====================================================

    if data == "balance":

        await query.message.reply_text(
            f"💰 موجودی شما:\n"
            f"{fmt(get_balance(user.id))}"
        )

        return

    # =====================================================
    # REFERRAL
    # =====================================================

    if data == "ref":

        bot = await context.bot.get_me()

        link = (
            f"https://t.me/{bot.username}"
            f"?start={user.id}"
        )

        await query.message.reply_text(

            "👥 سیستم زیرمجموعه\n\n"
            f"🔗 لینک شما:\n{link}\n\n"
            f"🎁 پاداش هر زیرمجموعه: "
            f"{fmt(REFERRAL_REWARD)}"
        )

        return

    # =====================================================
    # HELP
    # =====================================================

    if data == "help":

        await query.message.reply_text(

            "📖 راهنما\n\n"

            "🎲 1 تاس 0.1\n"
            "🎳 1 بولینگ 0.1\n"
            "🎯 1 دارت 0.1\n"
            "🏀 1 بسکتبال 0.1\n\n"

            "🤖 بازی با ربات:\n"
            "اول خود کاربر ایموجی بازی را می‌فرستد.\n"
            "بعد ربات خودش رول می‌کند.\n\n"

            "👥 بازی دوستان:\n"
            "اول سازنده رول می‌کند.\n"
            "بعد حریف رول می‌کند.\n\n"

            "💰 موجودی\n"
            "💸 انتقال 0.1 با Reply"
        )

        return

    # =====================================================
    # GAME CALLBACK
    # =====================================================

    match = re.match(
        r"^(bot|friend|cancel):(\d+)$",
        data
    )

    if match:

        action = match.group(1)
        game_id = int(match.group(2))

        con = connect()

        game = con.execute(
            """
            SELECT *
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        con.close()

        if not game or game["settled"]:

            await query.message.reply_text(
                "❌ بازی پیدا نشد یا تمام شده است."
            )

            return

        # =================================================
        # CANCEL
        # =================================================

        if action == "cancel":

            if (
                user.id != game["creator_id"]
                and not is_admin(user.id)
            ):

                await query.answer(
                    "❌ فقط سازنده می‌تواند لغو کند.",
                    show_alert=True
                )

                return

            if game["opponent_id"]:

                await refund_both(
                    game_id
                )

            else:

                await refund_game(
                    game_id,
                    game["creator_id"]
                )

            await query.message.edit_text(

                f"❌ بازی #{game_id} لغو شد.\n"
                "💰 مبلغ برگشت داده شد."
            )

            return

        # =================================================
        # BOT GAME
        # =================================================

        if action == "bot":

            if user.id != game["creator_id"]:

                await query.answer(
                    "❌ فقط سازنده بازی می‌تواند انتخاب کند.",
                    show_alert=True
                )

                return

            con = connect()

            con.execute(
                """
                UPDATE games
                SET mode='bot',
                    status='waiting_creator'
                WHERE id=?
                AND settled=0
                """,
                (game_id,)
            )

            con.commit()
            con.close()

            emoji = GAME_EMOJI[
                game["game_type"]
            ]

            await query.message.edit_text(

                f"🤖 بازی با ربات #{game_id}\n\n"

                f"👤 سازنده: {user_name(user)}\n\n"

                f"{emoji} حالا خودت "
                f"{emoji} را بفرست.\n\n"

                "⚠️ ربات تا وقتی خود کاربر رول نکند "
                "هیچ رولی انجام نمی‌دهد."
            )

            return

        # =================================================
        # FRIEND GAME
        # =================================================

        if action == "friend":

            if user.id == game["creator_id"]:

                await query.answer(
                    "❌ خود سازنده نمی‌تواند حریف خودش باشد.",
                    show_alert=True
                )

                return

            if game["opponent_id"]:

                await query.answer(
                    "❌ این بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            stake = float(
                game["stake"]
            )

            async with DB_LOCK:

                con = connect()
                cur = con.cursor()

                balance = cur.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """,
                    (user.id,)
                ).fetchone()

                if (
                    not balance
                    or float(balance["balance"]) < stake
                ):

                    con.close()

                    await query.answer(
                        "❌ موجودی کافی نیست.",
                        show_alert=True
                    )

                    return

                # کسر حریف
                cur.execute(
                    """
                    UPDATE users
                    SET balance=ROUND(balance-?,8)
                    WHERE user_id=?
                    AND balance>=?
                    """,
                    (
                        stake,
                        user.id,
                        stake
                    )
                )

                if cur.rowcount != 1:

                    con.rollback()
                    con.close()

                    await query.answer(
                        "❌ موجودی کافی نیست.",
                        show_alert=True
                    )

                    return

                cur.execute(
                    """
                    INSERT INTO transactions
                    (user_id,amount,kind,created_at)
                    VALUES(?,?,?,?)
                    """,
                    (
                        user.id,
                        -stake,
                        "game_stake",
                        time.time()
                    )
                )

                # گرفتن بازی به صورت اتمیک
                cur.execute(
                    """
                    UPDATE games
                    SET opponent_id=?,
                        mode='friend',
                        status='waiting_creator'
                    WHERE id=?
                    AND opponent_id IS NULL
                    AND settled=0
                    """,
                    (
                        user.id,
                        game_id
                    )
                )

                if cur.rowcount != 1:

                    # برگشت کسر حریف
                    cur.execute(
                        """
                        UPDATE users
                        SET balance=ROUND(balance+?,8)
                        WHERE user_id=?
                        """,
                        (
                            stake,
                            user.id
                        )
                    )

                    con.commit()
                    con.close()

                    await query.answer(
                        "❌ بازی قبلاً گرفته شده.",
                        show_alert=True
                    )

                    return

                con.commit()
                con.close()

            await query.message.edit_text(

                f"👥 بازی دوستان #{game_id}\n\n"

                f"👤 سازنده: {game['creator_id']}\n"
                f"👤 حریف: {user_name(user)}\n\n"

                "🎲 اول سازنده باید خودش رول کند.\n"
                "بعد حریف باید خودش رول کند.\n\n"

                "🤖 ربات در بازی دوستان رول نمی‌کند."
            )

            return


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin(update, context):

    user = update.effective_user

    if not is_admin(user.id):

        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    enabled = setting(
        "enabled",
        "1"
    ) == "1"

    keyboard = InlineKeyboardMarkup([

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
                callback_data="admin_sub"
            )
        ],

        [
            InlineKeyboardButton(
                "🎮 بازی‌های فعال",
                callback_data="admin_games"
            )
        ],

        [
            InlineKeyboardButton(
                "🔴 خاموش کردن"
                if enabled
                else "🟢 روشن کردن",
                callback_data="admin_toggle"
            )
        ]

    ])

    await update.message.reply_text(

        "👑 پنل مدیریت\n\n"
        "مدیریت کامل ربات:",

        reply_markup=keyboard
    )


# =========================================================
# ADMIN CALLBACKS
# =========================================================

async def admin_callback(query, context):

    user = query.from_user

    if not is_admin(user.id):
        return

    data = query.data

    # =====================================================
    # STATS
    # =====================================================

    if data == "admin_stats":

        con = connect()

        users = con.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        games = con.execute(
            """
            SELECT COUNT(*) c
            FROM games
            WHERE settled=0
            """
        ).fetchone()["c"]

        balance = con.execute(
            """
            SELECT COALESCE(SUM(balance),0) b
            FROM users
            """
        ).fetchone()["b"]

        con.close()

        await query.message.reply_text(

            "📊 آمار\n\n"
            f"👥 کاربران: {users}\n"
            f"🎮 بازی‌های فعال: {games}\n"
            f"💰 مجموع موجودی: {fmt(balance)}"
        )

        return

    # =====================================================
    # USERS
    # =====================================================

    if data == "admin_users":

        con = connect()

        rows = con.execute(
            """
            SELECT user_id,first_name,balance
            FROM users
            ORDER BY balance DESC
            LIMIT 20
            """
        ).fetchall()

        con.close()

        text = "👥 کاربران:\n\n"

        for row in rows:

            text += (
                f"🆔 {row['user_id']}\n"
                f"👤 {row['first_name']}\n"
                f"💰 {fmt(row['balance'])}\n\n"
            )

        await query.message.reply_text(
            text[:4000]
        )

        return

    # =====================================================
    # ADD
    # =====================================================

    if data == "admin_add":

        await query.message.reply_text(

            "➕ افزایش موجودی\n\n"

            "دستور:\n"
            "/addbalance USER_ID AMOUNT\n\n"

            "مثال:\n"
            "/addbalance 8552447077 1000"
        )

        return

    # =====================================================
    # SUB
    # =====================================================

    if data == "admin_sub":

        await query.message.reply_text(

            "➖ کسر موجودی\n\n"

            "دستور:\n"
            "/subbalance USER_ID AMOUNT\n\n"

            "مثال:\n"
            "/subbalance 8552447077 100"
        )

        return

    # =====================================================
    # TOGGLE
    # =====================================================

    if data == "admin_toggle":

        current = setting(
            "enabled",
            "1"
        )

        new_value = (
            "0"
            if current == "1"
            else "1"
        )

        set_setting(
            "enabled",
            new_value
        )

        await query.message.reply_text(

            "🟢 ربات روشن شد."
            if new_value == "1"
            else
            "🔴 ربات خاموش شد."
        )

        return

    # =====================================================
    # ACTIVE GAMES
    # =====================================================

    if data == "admin_games":

        con = connect()

        rows = con.execute(
            """
            SELECT id,game_type,stake,
                   creator_id,opponent_id,status
            FROM games
            WHERE settled=0
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()

        con.close()

        text = "🎮 بازی‌های فعال:\n\n"

        for row in rows:

            text += (
                f"#{row['id']} | "
                f"{row['game_type']} | "
                f"{fmt(row['stake'])}\n"
                f"سازنده: {row['creator_id']}\n"
                f"حریف: {row['opponent_id']}\n"
                f"وضعیت: {row['status']}\n\n"
            )

        await query.message.reply_text(
            text[:4000]
        )

        return


# =========================================================
# ADMIN TEXT COMMANDS
# =========================================================

async def admin_text(update, context):

    user = update.effective_user

    if not is_admin(user.id):
        return

    text = normalize(
        update.message.text.strip()
    )

    # =====================================================
    # ADD BALANCE
    # =====================================================

    match = re.match(
        r"^/addbalance\s+(\d+)\s+([0-9]+(?:\.[0-9]+)?)$",
        text,
        re.I
    )

    if match:

        uid = int(
            match.group(1)
        )

        amount = float(
            match.group(2)
        )

        ensure_user(uid)

        change_balance(
            uid,
            amount,
            "admin_add"
        )

        await update.message.reply_text(

            "✅ افزایش موجودی انجام شد.\n\n"
            f"🆔 {uid}\n"
            f"➕ {fmt(amount)}\n"
            f"💰 موجودی جدید: "
            f"{fmt(get_balance(uid))}"
        )

        return

    # =====================================================
    # SUB BALANCE
    # =====================================================

    match = re.match(
        r"^/subbalance\s+(\d+)\s+([0-9]+(?:\.[0-9]+)?)$",
        text,
        re.I
    )

    if match:

        uid = int(
            match.group(1)
        )

        amount = float(
            match.group(2)
        )

        if get_balance(uid) < amount:

            await update.message.reply_text(
                "❌ موجودی کاربر کافی نیست."
            )

            return

        change_balance(
            uid,
            -amount,
            "admin_sub"
        )

        await update.message.reply_text(

            "✅ کسر موجودی انجام شد.\n\n"
            f"🆔 {uid}\n"
            f"➖ {fmt(amount)}\n"
            f"💰 موجودی جدید: "
            f"{fmt(get_balance(uid))}"
        )


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(update, context):

    message = update.message

    if not message or not message.text:
        return

    user = update.effective_user

    ensure_user(
        user.id,
        user.first_name,
        user.username
    )

    text = message.text.strip()

    # =====================================================
    # ADMIN COMMANDS
    # =====================================================

    if (
        text.startswith("/addbalance")
        or text.startswith("/subbalance")
    ):

        await admin_text(
            update,
            context
        )

        return

    if not await require_join(
        update,
        context
    ):

        return

    # =====================================================
    # BALANCE
    # =====================================================

    if normalize(text).lower() in (
        "موجودی",
        "balance"
    ):

        await show_balance(
            update,
            context
        )

        return

    # =====================================================
    # TRANSFER
    # =====================================================

    transfer_amount = parse_transfer(
        text
    )

    if transfer_amount is not None:

        await do_transfer(
            update,
            context,
            transfer_amount
        )

        return

    # =====================================================
    # GAME
    # =====================================================

    parsed = parse_game(
        text
    )

    if parsed:

        if update.effective_chat.type == ChatType.PRIVATE:

            await message.reply_text(
                "❌ بازی‌ها فقط داخل گپ انجام می‌شوند."
            )

            return

        game_type, amount = parsed

        await create_game(
            update,
            context,
            game_type,
            amount
        )


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    log.exception(
        "BOT ERROR",
        exc_info=context.error
    )

    try:

        if update and update.effective_message:

            await update.effective_message.reply_text(
                "⚠️ خطای موقت رخ داد."
            )

    except Exception:

        pass


# =========================================================
# CALLBACK DISPATCHER
# =========================================================

async def callback_dispatcher(update, context):

    query = update.callback_query

    if query.data.startswith("admin_"):

        await query.answer()

        await admin_callback(
            query,
            context
        )

        return

    await callbacks(
        update,
        context
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است. "
            "در GitHub Actions بخش Secrets مقدار BOT_TOKEN را قرار بده."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # دستورات انگلیسی تلگرام
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

    # Callback ها
    app.add_handler(
        CallbackQueryHandler(
            callback_dispatcher
        )
    )

    # رول واقعی تلگرام توسط کاربر
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            handle_roll
        )
    )

    # متن‌ها
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    # دستورات افزایش/کسر موجودی
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/addbalance\s+"),
            admin_text
        ),
        group=1
    )

    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/subbalance\s+"),
            admin_text
        ),
        group=1
    )

    app.add_error_handler(
        error_handler
    )

    log.info(
        "BET_BT started successfully."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
