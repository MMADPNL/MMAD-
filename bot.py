# bot.py
# Python 3.10+
# pip install python-telegram-bot==22.5

import os
import re
import sqlite3
import secrets
import logging
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB_FILE = "BET_BT.db"

REF_REWARD = 0.05

WIN_RATE = 0.925

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("BET_BT")


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
    return con


def init_db():

    with closing(db()) as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                balance REAL NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referral_earned REAL NOT NULL DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                games INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                draws INTEGER NOT NULL DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id TEXT PRIMARY KEY,

                chat_id INTEGER NOT NULL,

                creator_id INTEGER NOT NULL,
                opponent_id INTEGER DEFAULT NULL,

                game_type TEXT NOT NULL,
                emoji TEXT NOT NULL,

                rounds INTEGER NOT NULL,
                stake REAL NOT NULL,

                creator_round INTEGER NOT NULL DEFAULT 0,
                opponent_round INTEGER NOT NULL DEFAULT 0,

                creator_score INTEGER NOT NULL DEFAULT 0,
                opponent_score INTEGER NOT NULL DEFAULT 0,

                robot INTEGER NOT NULL DEFAULT 0,

                status TEXT NOT NULL DEFAULT 'waiting',
                settled INTEGER NOT NULL DEFAULT 0
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        con.execute("""
            INSERT OR IGNORE INTO settings
            (key,value)
            VALUES
            ('enabled','1')
        """)

        con.commit()


# ============================================================
# USER
# ============================================================

def ensure_user(user):

    if not user:
        return

    with closing(db()) as con:

        con.execute("""
            INSERT INTO users(
                user_id,
                name,
                username
            )
            VALUES(?,?,?)

            ON CONFLICT(user_id)
            DO UPDATE SET
                name=excluded.name,
                username=excluded.username
        """, (
            user.id,
            user.full_name or "",
            user.username or ""
        ))

        con.commit()


def get_user(user_id):

    with closing(db()) as con:

        return con.execute("""
            SELECT *
            FROM users
            WHERE user_id=?
        """, (
            user_id,
        )).fetchone()


def get_balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0.0

    return float(row["balance"])


def fmt(value):

    value = round(float(value), 8)

    return (
        f"{value:.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


def normalize(text):

    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


# ============================================================
# BALANCE
# ============================================================

def change_balance(user_id, amount):

    amount = round(float(amount), 8)

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (
                user_id,
            )).fetchone()

            if not row:
                con.rollback()
                return False

            old = float(row["balance"])
            new = round(old + amount, 8)

            if new < 0:

                con.rollback()
                return False

            con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
            """, (
                new,
                user_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()

            log.exception("balance update failed")

            return False


# ============================================================
# SETTINGS
# ============================================================

def enabled():

    with closing(db()) as con:

        row = con.execute("""
            SELECT value
            FROM settings
            WHERE key='enabled'
        """).fetchone()

    return not row or row["value"] == "1"


def set_enabled(value):

    with closing(db()) as con:

        con.execute("""
            INSERT INTO settings(key,value)
            VALUES('enabled',?)

            ON CONFLICT(key)
            DO UPDATE SET
                value=excluded.value
        """, (
            "1" if value else "0",
        ))

        con.commit()


# ============================================================
# JOIN
# ============================================================

async def is_joined(user_id, bot):

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

    except Exception as e:

        log.warning(
            "join check failed: %s",
            e
        )

        return False


def join_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="join_check"
            )
        ]
    ])


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard(user_id):

    rows = [

        [
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="ref"
            )
        ],

        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance"
            ),

            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer"
            )
        ],

        [
            InlineKeyboardButton(
                "🎮 مثال بازی",
                callback_data="examples"
            )
        ]
    ]

    if user_id == OWNER_ID:

        rows.append([
            InlineKeyboardButton(
                "👑 پنل مدیریت",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # referral
    if context.args:

        try:

            ref_id = int(
                normalize(
                    context.args[0]
                )
            )

            if (
                ref_id != user.id
                and get_user(ref_id)
            ):

                with closing(db()) as con:

                    row = con.execute("""
                        SELECT referred_by
                        FROM users
                        WHERE user_id=?
                    """, (
                        user.id,
                    )).fetchone()

                    if (
                        row
                        and row["referred_by"] is None
                    ):

                        con.execute("""
                            UPDATE users
                            SET referred_by=?
                            WHERE user_id=?
                        """, (
                            ref_id,
                            user.id
                        ))

                        con.execute("""
                            UPDATE users
                            SET
                                referrals=referrals+1,
                                referral_earned=
                                    referral_earned+?,
                                balance=balance+?
                            WHERE user_id=?
                        """, (
                            REF_REWARD,
                            REF_REWARD,
                            ref_id
                        ))

                        con.commit()

        except Exception:
            pass

    if not await is_joined(
        user.id,
        context.bot
    ):

        await update.message.reply_text(
            "برای استفاده ابتدا عضو کانال شو.",
            reply_markup=join_keyboard()
        )

        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=main_keyboard(
            user.id
        )
    )


# ============================================================
# BALANCE BUTTON
# ============================================================

async def balance_button(
    update,
    context
):

    query = update.callback_query

    user = update.effective_user

    ensure_user(user)

    await query.answer()

    await query.message.reply_text(
        "💰 موجودی شما:\n\n"
        f"{fmt(get_balance(user.id))} TRX بازی"
    )


# ============================================================
# BALANCE IN GROUP
# ============================================================

async def balance_text(
    update,
    context
):

    message = update.message

    user = update.effective_user

    if not message:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    if normalize(
        message.text.strip()
    ) != "موجودی":

        return

    ensure_user(user)

    await message.reply_text(
        "💰 موجودی شما:\n\n"
        f"{fmt(get_balance(user.id))} TRX بازی"
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral_button(
    update,
    context
):

    query = update.callback_query

    user = update.effective_user

    ensure_user(user)

    row = get_user(user.id)

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start={user.id}"
    )

    await query.answer()

    await query.message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"👤 تعداد: {row['referrals']}\n"
        f"🎁 هر نفر: {fmt(REF_REWARD)}\n"
        f"💰 دریافتی: "
        f"{fmt(row['referral_earned'])}\n\n"
        "🔗 لینک دعوت:\n"
        f"{link}"
    )


# ============================================================
# EXAMPLES
# ============================================================

async def examples_button(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🎮 مثال بازی\n\n"

        "🎲 4 تاس 0.1\n"
        "🎲 10 تاس 0.1\n"
        "🎲 100 تاس 0.1\n\n"

        "🎯 4 دارت 0.1\n"
        "🎯 10 دارت 0.1\n\n"

        "🏀 4 بسکتبال 0.1\n"
        "🏀 10 بسکتبال 0.1\n\n"

        "🎳 4 بولینگ 0.1\n"
        "🎳 10 بولینگ 0.1\n\n"

        "اعداد فارسی هم قبول است:\n"
        "4 تاس ۰.۱\n\n"

        "🔢 تعداد راند نامحدود است."
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_button(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 1\n\n"
        "یا:\n"
        "انتقال 0.1"
    )


async def transfer_handler(
    update,
    context
):

    message = update.message

    user = update.effective_user

    if not message.reply_to_message:
        return

    if not message.text:
        return

    text = normalize(
        message.text.strip()
    )

    if not text.startswith("انتقال"):
        return

    target = (
        message.reply_to_message
        .from_user
    )

    if not target:
        return

    if target.is_bot:
        return

    if target.id == user.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    ensure_user(user)
    ensure_user(target)

    parts = text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ فرمت:\n"
            "انتقال 1"
        )

        return

    try:

        amount = float(
            parts[1]
        )

    except ValueError:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    amount = round(
        amount,
        8
    )

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

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
                SELECT user_id
                FROM users
                WHERE user_id=?
            """, (
                target.id,
            )).fetchone()

            if not sender or not receiver:

                con.rollback()

                return

            if float(sender["balance"]) < amount:

                con.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            con.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
            """, (
                amount,
                user.id
            ))

            con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                amount,
                target.id
            ))

            con.commit()

        except Exception:

            con.rollback()

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {target.full_name}\n"
        f"💰 مقدار: {fmt(amount)} TRX بازی"
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_TYPES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳"
}


def parse_game(text):

    text = normalize(
        text.strip()
    )

    pattern = (
        r"^(\d+)\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)$"
    )

    match = re.match(
        pattern,
        text
    )

    if not match:
        return None

    rounds = int(
        match.group(1)
    )

    game_type = match.group(2)

    stake = float(
        match.group(3)
    )

    if rounds <= 0:
        return None

    if stake <= 0:
        return None

    return (
        rounds,
        game_type,
        GAME_TYPES[game_type],
        round(stake, 8)
    )


# ============================================================
# ACTIVE GAME
# ============================================================

def active_game(user_id):

    with closing(db()) as con:

        return con.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND status IN(
                'waiting',
                'playing',
                'robot'
            )
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY rowid DESC
            LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()


# ============================================================
# GAME KEYBOARD
# ============================================================

def game_keyboard(game_id):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"robot:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]

    ])


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context
):

    message = update.message

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        message.text
    )

    if not parsed:
        return

    user = update.effective_user

    ensure_user(user)

    if not enabled():
        return

    if active_game(user.id):

        await message.reply_text(
            "❌ شما در حال بازی هستید.\n"
            "ابتدا بازی قبلی تمام شود."
        )

        return

    rounds, game_type, emoji, stake = parsed

    if get_balance(user.id) < stake:

        await message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{fmt(get_balance(user.id))} TRX بازی\n"
            f"💰 شرط: "
            f"{fmt(stake)} TRX بازی"
        )

        return

    if not change_balance(
        user.id,
        -stake
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    game_id = secrets.token_hex(12)

    with closing(db()) as con:

        try:

            con.execute("""
                INSERT INTO games(
                    id,
                    chat_id,
                    creator_id,
                    game_type,
                    emoji,
                    rounds,
                    stake
                )
                VALUES(?,?,?,?,?,?,?)
            """, (
                game_id,
                message.chat.id,
                user.id,
                game_type,
                emoji,
                rounds,
                stake
            ))

            con.commit()

        except Exception:

            con.rollback()

            change_balance(
                user.id,
                stake
            )

            await message.reply_text(
                "❌ ساخت بازی ناموفق بود."
            )

            return

    await message.reply_text(

        f"{emoji} بازی ساخته شد.\n\n"

        f"🎮 بازی: {game_type}\n"
        f"🔢 تعداد راند: {rounds}\n"
        f"💰 مبلغ ورود: "
        f"{fmt(stake)} TRX بازی\n\n"

        "انتخاب کن:",

        reply_markup=game_keyboard(
            game_id
        )
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game(
    update,
    context
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1
    )[1]

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی شروع شده.",
            show_alert=True
        )

        return

    if game["creator_id"] == user.id:

        await query.answer(
            "❌ خودت سازنده‌ای.",
            show_alert=True
        )

        return

    if active_game(user.id):

        await query.answer(
            "❌ شما در حال بازی هستید.",
            show_alert=True
        )

        return

    ensure_user(user)

    stake = float(
        game["stake"]
    )

    if get_balance(user.id) < stake:

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    if not change_balance(
        user.id,
        -stake
    ):

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            current = con.execute("""
                SELECT status, settled
                FROM games
                WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if (
                not current
                or current["status"] != "waiting"
                or current["settled"]
            ):

                con.rollback()

                change_balance(
                    user.id,
                    stake
                )

                await query.answer(
                    "❌ بازی پر شده.",
                    show_alert=True
                )

                return

            con.execute("""
                UPDATE games
                SET
                    opponent_id=?,
                    status='playing'
                WHERE id=?
            """, (
                user.id,
                game_id
            ))

            con.commit()

        except Exception:

            con.rollback()

            change_balance(
                user.id,
                stake
            )

            await query.answer(
                "❌ خطا.",
                show_alert=True
            )

            return

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.edit_message_text(

        f"{game['emoji']} بازی شروع شد.\n\n"

        f"🔢 راندها: {game['rounds']}\n"
        f"💰 مبلغ ورود: "
        f"{fmt(stake)} TRX بازی\n\n"

        "هر دو بازیکن باید خودشان "
        f"{game['emoji']} را بفرستند."
    )


# ============================================================
# ROBOT
# ============================================================

async def robot_game(
    update,
    context
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1
    )[1]

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ بازی قابل شروع نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        con.execute("""
            UPDATE games
            SET
                robot=1,
                opponent_id=0,
                status='robot'
            WHERE id=?
        """, (
            game_id,
        ))

        con.commit()

    await query.answer()

    await query.edit_message_text(

        f"🤖 بازی با ربات\n\n"

        f"🎮 {game['game_type']}\n"
        f"🔢 تعداد راند: {game['rounds']}\n"
        f"💰 ورود: "
        f"{fmt(game['stake'])} TRX بازی\n\n"

        f"اول تو {game['emoji']} را بفرست.\n"
        "بعد ربات پرتاب می‌کند."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(
    update,
    context
):

    query = update.callback_query

    user = update.effective_user

    game_id = query.data.split(
        ":",
        1
    )[1]

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده.",
            show_alert=True
        )

        return

    if game["status"] not in (
        "waiting",
        "robot"
    ):

        await query.answer(
            "❌ قابل لغو نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        con.execute("""
            UPDATE games
            SET
                status='cancelled',
                settled=1
            WHERE id=?
            AND settled=0
        """, (
            game_id,
        ))

        con.commit()

    change_balance(
        user.id,
        float(game["stake"])
    )

    await query.answer(
        "❌ لغو شد."
    )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        "💰 مبلغ ورود برگشت داده شد."
    )


# ============================================================
# GET GAME
# ============================================================

def get_game(game_id):

    with closing(db()) as con:

        return con.execute("""
            SELECT *
            FROM games
            WHERE id=?
        """, (
            game_id,
        )).fetchone()


# ============================================================
# DICE / DART / BASKETBALL / BOWLING
# ============================================================

async def dice_handler(
    update,
    context
):

    message = update.message

    if not message:
        return

    if not message.dice:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    emoji = message.dice.emoji

    if emoji not in (
        "🎲",
        "🎯",
        "🏀",
        "🎳"
    ):
        return

    user = update.effective_user

    ensure_user(user)

    with closing(db()) as con:

        game = con.execute("""
            SELECT *
            FROM games
            WHERE settled=0
            AND emoji=?
            AND status IN(
                'playing',
                'robot'
            )
            AND(
                creator_id=?
                OR opponent_id=?
            )
            ORDER BY rowid DESC
            LIMIT 1
        """, (
            emoji,
            user.id,
            user.id
        )).fetchone()

    if not game:
        return

    value = int(
        message.dice.value
    )

    # ========================================================
    # ROBOT
    # ========================================================

    if game["robot"]:

        if user.id != game["creator_id"]:
            return

        if game["status"] != "robot":
            return

        round_no = (
            int(game["creator_round"]) + 1
        )

        creator_score = (
            int(game["creator_score"])
            + value
        )

        with closing(db()) as con:

            con.execute("""
                UPDATE games
                SET
                    creator_round=?,
                    creator_score=?
                WHERE id=?
                AND settled=0
            """, (
                round_no,
                creator_score,
                game["id"]
            ))

            con.commit()

        await message.reply_text(
            f"👤 راند {round_no}: {value}\n"
            f"📊 مجموع تو: {creator_score}"
        )

        try:

            robot_message = (
                await context.bot.send_dice(
                    chat_id=message.chat.id,
                    emoji=emoji
                )
            )

            robot_value = int(
                robot_message.dice.value
            )

        except Exception:

            await message.reply_text(
                "❌ پرتاب ربات انجام نشد."
            )

            return

        with closing(db()) as con:

            con.execute("""
                UPDATE games
                SET
                    opponent_round=
                        opponent_round+1,
                    opponent_score=
                        opponent_score+?
                WHERE id=?
                AND settled=0
            """, (
                robot_value,
                game["id"]
            ))

            con.commit()

        await message.reply_text(
            f"🤖 ربات: {robot_value}"
        )

        if round_no >= int(
            game["rounds"]
        ):

            await finish_game(
                game["id"],
                context
            )

        return

    # ========================================================
    # FRIEND
    # ========================================================

    if game["status"] != "playing":
        return

    if user.id == game["creator_id"]:

        with closing(db()) as con:

            con.execute("""
                UPDATE games
                SET
                    creator_round=
                        creator_round+1,
                    creator_score=
                        creator_score+?
                WHERE id=?
                AND settled=0
            """, (
                value,
                game["id"]
            ))

            con.commit()

        await message.reply_text(
            f"👤 پرتاب ثبت شد: {value}"
        )

    elif user.id == game["opponent_id"]:

        with closing(db()) as con:

            con.execute("""
                UPDATE games
                SET
                    opponent_round=
                        opponent_round+1,
                    opponent_score=
                        opponent_score+?
                WHERE id=?
                AND settled=0
            """, (
                value,
                game["id"]
            ))

            con.commit()

        await message.reply_text(
            f"👤 پرتاب ثبت شد: {value}"
        )

    else:
        return

    current = get_game(
        game["id"]
    )

    if not current:
        return

    if (
        current["creator_round"]
        >= current["rounds"]
        and
        current["opponent_round"]
        >= current["rounds"]
    ):

        await finish_game(
            current["id"],
            context
        )


# ============================================================
# FINISH
# ============================================================

async def finish_game(
    game_id,
    context
):

    game = get_game(
        game_id
    )

    if not game:
        return

    if game["settled"]:
        return

    creator_score = int(
        game["creator_score"]
    )

    opponent_score = int(
        game["opponent_score"]
    )

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            current = con.execute("""
                SELECT settled
                FROM games
                WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if (
                not current
                or current["settled"]
            ):

                con.rollback()
                return

            con.execute("""
                UPDATE games
                SET
                    status='finished',
                    settled=1
                WHERE id=?
            """, (
                game_id,
            ))

            con.commit()

        except Exception:

            con.rollback()
            return

    stake = float(
        game["stake"]
    )

    pool = stake * 2

    # ========================================================
    # DRAW
    # ========================================================

    if creator_score == opponent_score:

        change_balance(
            game["creator_id"],
            stake
        )

        if game["opponent_id"]:

            change_balance(
                game["opponent_id"],
                stake
            )

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_score}\n"
            f"👤 بازیکن دوم: {opponent_score}\n\n"
            "🤝 مساوی شد.\n"
            "💰 مبلغ ورود برگشت داده شد."
        )

    # ========================================================
    # CREATOR WIN
    # ========================================================

    elif creator_score > opponent_score:

        reward = round(
            pool * WIN_RATE,
            8
        )

        change_balance(
            game["creator_id"],
            reward
        )

        with closing(db()) as con:

            con.execute("""
                UPDATE users
                SET
                    games=games+1,
                    wins=wins+1
                WHERE user_id=?
            """, (
                game["creator_id"],
            ))

            if game["opponent_id"]:

                con.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        losses=losses+1
                    WHERE user_id=?
                """, (
                    game["opponent_id"],
                ))

            con.commit()

        text = (
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 بازیکن اول: {creator_score}\n"
            f"👤 بازیکن دوم: {opponent_score}\n\n"
            "🏆 بازیکن اول برنده شد."
        )

    # ========================================================
    # OPPONENT WIN
    # ========================================================

    else:

        if game["opponent_id"]:

            reward = round(
                pool * WIN_RATE,
                8
            )

            change_balance(
                game["opponent_id"],
                reward
            )

            with closing(db()) as con:

                con.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        losses=losses+1
                    WHERE user_id=?
                """, (
                    game["creator_id"],
                ))

                con.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        wins=wins+1
                    WHERE user_id=?
                """, (
                    game["opponent_id"],
                ))

                con.commit()

            text = (
                f"{game['emoji']} نتیجه نهایی\n\n"
                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"
                "🏆 بازیکن دوم برنده شد."
            )

        else:

            text = (
                f"{game['emoji']} نتیجه نهایی\n\n"
                f"👤 تو: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"
                "🤖 ربات برنده شد."
            )

            with closing(db()) as con:

                con.execute("""
                    UPDATE users
                    SET
                        games=games+1,
                        losses=losses+1
                    WHERE user_id=?
                """, (
                    game["creator_id"],
                ))

                con.commit()

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# ============================================================
# ADMIN
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add"
            ),

            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove"
            )
        ],

        [
            InlineKeyboardButton(
                "💰 موجودی کاربر",
                callback_data="admin_balance"
            )
        ],

        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ],

        [
            InlineKeyboardButton(
                "🟢 روشن",
                callback_data="admin_on"
            ),

            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_off"
            )
        ],

        [
            InlineKeyboardButton(
                "🔙 منوی اصلی",
                callback_data="home"
            )
        ]
    ])


async def admin_button(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    await query.answer()

    await query.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard()
    )


async def admin_add(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()

    context.user_data["admin"] = "add"

    await query.answer()

    await query.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_remove(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()

    context.user_data["admin"] = "remove"

    await query.answer()

    await query.message.reply_text(
        "➖ کسر موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


async def admin_balance(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    context.user_data.clear()

    context.user_data["admin"] = "balance"

    await query.answer()

    await query.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    with closing(db()) as con:

        users = con.execute("""
            SELECT COUNT(*) c
            FROM users
        """).fetchone()["c"]

        games = con.execute("""
            SELECT COUNT(*) c
            FROM games
        """).fetchone()["c"]

        total = con.execute("""
            SELECT COALESCE(
                SUM(balance),
                0
            ) total
            FROM users
        """).fetchone()["total"]

    await query.answer()

    await query.message.reply_text(
        "📊 آمار\n\n"
        f"👥 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"💰 مجموع موجودی: {fmt(total)}\n"
        f"🔌 وضعیت: "
        f"{'🟢 روشن' if enabled() else '🔴 خاموش'}"
    )


async def admin_on(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    set_enabled(True)

    await query.answer(
        "🟢 روشن شد."
    )

    await query.message.reply_text(
        "🟢 ربات روشن شد.",
        reply_markup=admin_keyboard()
    )


async def admin_off(
    update,
    context
):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        return

    set_enabled(False)

    await query.answer(
        "🔴 خاموش شد."
    )

    await query.message.reply_text(
        "🔴 ربات خاموش شد.",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text(
    update,
    context
):

    user = update.effective_user

    if user.id != OWNER_ID:
        return

    action = context.user_data.get(
        "admin"
    )

    if not action:
        return

    text = normalize(
        update.message.text.strip()
    )

    # --------------------------------------------------------
    # BALANCE LOOKUP
    # --------------------------------------------------------

    if action == "balance":

        try:

            target_id = int(text)

        except ValueError:

            await update.message.reply_text(
                "❌ آیدی نامعتبر است."
            )

            return

        row = get_user(
            target_id
        )

        if not row:

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

            return

        await update.message.reply_text(
            "💰 اطلاعات کاربر\n\n"
            f"🆔 {target_id}\n"
            f"👤 {row['name']}\n"
            f"💰 موجودی: "
            f"{fmt(row['balance'])} TRX بازی\n"
            f"👥 رفرال: {row['referrals']}\n"
            f"🎮 بازی: {row['games']}\n"
            f"🏆 برد: {row['wins']}\n"
            f"❌ باخت: {row['losses']}\n"
            f"🤝 مساوی: {row['draws']}"
        )

        context.user_data.clear()

        return

    # --------------------------------------------------------
    # ADD / REMOVE
    # --------------------------------------------------------

    if action not in (
        "add",
        "remove"
    ):
        return

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ\n\n"
            "مثال:\n"
            "123456789 10"
        )

        return

    try:

        target_id = int(parts[0])
        amount = float(parts[1])

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار نامعتبر است."
        )

        return

    amount = round(
        amount,
        8
    )

    if amount <= 0:

        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد.\n"
            "ابتدا باید /start بزند."
        )

        return

    if action == "add":

        success = change_balance(
            target_id,
            amount
        )

        title = "افزایش"

    else:

        success = change_balance(
            target_id,
            -amount
        )

        title = "کسر"

    if not success:

        await update.message.reply_text(
            "❌ عملیات انجام نشد.\n"
            "ممکن است موجودی برای کسر کافی نباشد."
        )

        return

    await update.message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 کاربر: {target_id}\n"
        f"💰 مقدار: {fmt(amount)} TRX بازی\n"
        f"💰 موجودی جدید: "
        f"{fmt(get_balance(target_id))} TRX بازی"
    )

    context.user_data.clear()


# ============================================================
# HOME
# ============================================================

async def home_button(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    await query.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(
            query.from_user.id
        )
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(
    update,
    context
):

    query = update.callback_query

    data = query.data

    if data == "join_check":

        if await is_joined(
            query.from_user.id,
            context.bot
        ):

            await query.answer(
                "✅ عضویت تأیید شد."
            )

            await query.message.reply_text(
                "✅ تأیید شد.",
                reply_markup=main_keyboard(
                    query.from_user.id
                )
            )

        else:

            await query.answer(
                "❌ هنوز عضو نیستی.",
                show_alert=True
            )

        return

    if data == "balance":

        await balance_button(
            update,
            context
        )

    elif data == "ref":

        await referral_button(
            update,
            context
        )

    elif data == "examples":

        await examples_button(
            update,
            context
        )

    elif data == "transfer":

        await transfer_button(
            update,
            context
        )

    elif data == "admin":

        await admin_button(
            update,
            context
        )

    elif data == "admin_add":

        await admin_add(
            update,
            context
        )

    elif data == "admin_remove":

        await admin_remove(
            update,
            context
        )

    elif data == "admin_balance":

        await admin_balance(
            update,
            context
        )

    elif data == "admin_stats":

        await admin_stats(
            update,
            context
        )

    elif data == "admin_on":

        await admin_on(
            update,
            context
        )

    elif data == "admin_off":

        await admin_off(
            update,
            context
        )

    elif data == "home":

        await home_button(
            update,
            context
        )

    elif data.startswith("join:"):

        await join_game(
            update,
            context
        )

    elif data.startswith("robot:"):

        await robot_game(
            update,
            context
        )

    elif data.startswith("cancel:"):

        await cancel_game(
            update,
            context
        )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(
    update,
    context
):

    if not update.message:
        return

    text = update.message.text

    if not text:
        return

    user = update.effective_user

    ensure_user(user)

    # ADMIN
    if (
        user.id == OWNER_ID
        and context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context
        )

        return

    # BALANCE
    if normalize(
        text.strip()
    ) == "موجودی":

        await balance_text(
            update,
            context
        )

        return

    # TRANSFER
    if normalize(
        text.strip()
    ).startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # GAME
    if parse_game(text):

        await create_game(
            update,
            context
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    log.error(
        "Unhandled error:",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN را در Environment Variables قرار بده."
        )

    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # buttons
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # game emojis
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # text
    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router
        )
    )

    app.add_error_handler(
        error_handler
    )

    log.info(
        "BET_BT is running..."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
