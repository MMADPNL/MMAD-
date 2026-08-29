import os
import re
import sqlite3
import secrets
import logging
import asyncio
from contextlib import closing

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

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB = "bot.sqlite3"

# حداکثر تعداد دور
MAX_ROUNDS = 100

# حداکثر مبلغ
MAX_AMOUNT = 1_000_000

# =========================================================
# LOG
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("BET_BT")


# =========================================================
# DATABASE
# =========================================================

def db():
    con = sqlite3.connect(
        DB,
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
            balance REAL DEFAULT 0
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,

            creator INTEGER NOT NULL,
            opponent INTEGER DEFAULT NULL,

            game TEXT NOT NULL,
            emoji TEXT NOT NULL,

            rounds INTEGER NOT NULL,
            amount REAL DEFAULT 0,

            creator_round INTEGER DEFAULT 0,
            opponent_round INTEGER DEFAULT 0,

            creator_score INTEGER DEFAULT 0,
            opponent_score INTEGER DEFAULT 0,

            mode TEXT NOT NULL,
            status TEXT NOT NULL,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.commit()

        # سازگاری با دیتابیس قدیمی
        columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        if "amount" not in columns:
            con.execute("""
            ALTER TABLE games
            ADD COLUMN amount REAL DEFAULT 0
            """)

        con.commit()


# =========================================================
# USER
# =========================================================

def register(user):

    if not user:
        return

    with closing(db()) as con:

        con.execute("""
        INSERT INTO users(
            user_id,
            name,
            username
        )
        VALUES (?, ?, ?)

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


def balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0.0

    try:
        return max(
            0.0,
            float(row["balance"])
        )
    except Exception:
        return 0.0


def money(value):

    try:
        value = float(value)
    except Exception:
        return "0"

    return (
        f"{value:.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# =========================================================
# DIGITS
# =========================================================

def digits(text):

    if text is None:
        return ""

    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


# =========================================================
# NAME
# =========================================================

def display_name(user_id):

    row = get_user(user_id)

    if not row:
        return f"کاربر {user_id}"

    name = row["name"] or ""
    username = row["username"] or ""

    if name:
        return name

    if username:
        return f"@{username}"

    return f"کاربر {user_id}"


# =========================================================
# GAMES
# =========================================================

GAMES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳",
}


# =========================================================
# VALID TELEGRAM VALUES
# =========================================================

def valid_game_value(emoji, value):

    try:
        value = int(value)
    except Exception:
        return False

    if emoji == "🎲":
        return 1 <= value <= 6

    if emoji == "🎯":
        return 1 <= value <= 6

    if emoji == "🏀":
        return 1 <= value <= 5

    if emoji == "🎳":
        return 1 <= value <= 6

    return False


# =========================================================
# PARSE GAME
# =========================================================

def parse_game(text):

    if not text:
        return None

    text = digits(text.strip())

    pattern = (
        r"^(\d{1,3})\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d{1,8}(?:\.\d{1,8})?)$"
    )

    match = re.fullmatch(
        pattern,
        text
    )

    if not match:
        return None

    try:
        rounds = int(match.group(1))
        amount = float(match.group(3))
    except Exception:
        return None

    game = match.group(2)

    if rounds < 1 or rounds > MAX_ROUNDS:
        return None

    if amount <= 0 or amount > MAX_AMOUNT:
        return None

    return {
        "rounds": rounds,
        "game": game,
        "emoji": GAMES[game],
        "amount": round(amount, 8)
    }


# =========================================================
# GET GAME
# =========================================================

def get_game(game_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE id=?
        """, (
            game_id,
        )).fetchone()


# =========================================================
# USER ACTIVE GAME
# =========================================================

def user_game(user_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE status IN ('waiting', 'playing')
        AND (
            creator=?
            OR opponent=?
        )
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()


# =========================================================
# MEMBERSHIP
# =========================================================

async def member_ok(bot, user_id):

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

        # اگر بات دسترسی بررسی کانال نداشت،
        # بازی از کار نیفتد.
        return True


async def membership(update, context):

    user = update.effective_user

    if not user:
        return False

    if await member_ok(
        context.bot,
        user.id
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="membership"
            )
        ]
    ])

    if update.message:

        await update.message.reply_text(
            "❌ ابتدا عضو کانال شوید.",
            reply_markup=keyboard
        )

    elif update.callback_query:

        await update.callback_query.answer(
            "❌ ابتدا عضو کانال شوید.",
            show_alert=True
        )

    return False


# =========================================================
# MAIN KEYBOARD
# =========================================================

def keyboard(user_id):

    rows = [
        [
            InlineKeyboardButton(
                "🎮 بازی",
                callback_data="games"
            ),
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance"
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer"
            ),
            InlineKeyboardButton(
                "💸 برداشت",
                callback_data="withdraw"
            )
        ],
        [
            InlineKeyboardButton(
                "📖 راهنما",
                callback_data="help"
            ),
            InlineKeyboardButton(
                "🎯 مثال بازی",
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


# =========================================================
# START
# =========================================================

async def start(update, context):

    if not update.message:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "💰 واحد حساب: TRX داخلی بات\n"
        "⛓ بدون بلاکچین\n"
        "💳 بدون موجودی واقعی\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=keyboard(user.id)
    )


# =========================================================
# GAMES MENU
# =========================================================

async def games(update, context):

    q = update.callback_query

    await q.answer()

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data="friends"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data="robot_help"
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 مثال‌ها",
                callback_data="examples"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="home"
            )
        ]
    ])

    await q.message.reply_text(
        "🎮 بازی‌ها\n\n"
        "ساخت بازی داخل گپ:\n\n"
        "3 تاس 100\n"
        "3 دارت 100\n"
        "3 بسکتبال 100\n"
        "3 بولینگ 100\n\n"
        "هر ۴ بازی همین ترتیب را دارند:\n"
        "👤 اول کاربر تمام پرتاب‌ها\n"
        "🤖 بعد ربات تمام پرتاب‌ها",
        reply_markup=kb
    )


# =========================================================
# EXAMPLES
# =========================================================

async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال بازی:\n\n"
        "3 تاس 100\n"
        "3 دارت 100\n"
        "3 بسکتبال 100\n"
        "3 بولینگ 100\n\n"
        "حداکثر دور: 100\n"
        "واحد: TRX داخلی"
    )


# =========================================================
# FRIENDS
# =========================================================

async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "داخل گپ بنویس:\n\n"
        "3 تاس 100\n\n"
        "بعد نفر دوم «ورود به بازی» را می‌زند.\n\n"
        "در بازی دوستان، هر دور به ترتیب:\n"
        "👤 سازنده\n"
        "👤 حریف\n\n"
        "و این ترتیب تا پایان ادامه دارد."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "هر ۴ بازی یکسان کار می‌کنند:\n\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "اول تمام پرتاب‌های کاربر ثبت می‌شود.\n"
        "بعد ربات تمام پرتاب‌های خودش را انجام می‌دهد.\n"
        "سپس نتیجه اعلام می‌شود."
    )


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(update, context):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        msg.text or ""
    )

    if not parsed:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    # یک بازی فعال برای هر کاربر
    existing = user_game(user.id)

    if existing:

        await msg.reply_text(
            "❌ شما یک بازی فعال دارید.\n"
            "ابتدا همان بازی را تمام کنید."
        )

        return

    amount = parsed["amount"]
    game_id = secrets.token_hex(16)

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                user.id,
            )).fetchone()

            if not row:

                con.rollback()

                await msg.reply_text(
                    "❌ حساب شما پیدا نشد."
                )

                return

            current = float(row["balance"])

            if current < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: {money(current)} TRX\n"
                    f"🎯 شرط: {money(amount)} TRX"
                )

                return

            # کسر شرط سازنده
            cur = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if cur.rowcount != 1:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی تغییر کرده؛ دوباره تلاش کن."
                )

                return

            con.execute("""
            INSERT INTO games(
                id,
                chat_id,
                creator,
                opponent,
                game,
                emoji,
                rounds,
                amount,
                creator_round,
                opponent_round,
                creator_score,
                opponent_score,
                mode,
                status
            )
            VALUES(
                ?, ?, ?, NULL, ?, ?, ?, ?,
                0, 0, 0, 0,
                'friend',
                'waiting'
            )
            """, (
                game_id,
                msg.chat.id,
                user.id,
                parsed["game"],
                parsed["emoji"],
                parsed["rounds"],
                amount
            ))

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "create_game error"
            )

            await msg.reply_text(
                "❌ خطا در ساخت بازی."
            )

            return

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 ورود به بازی",
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

    await msg.reply_text(
        f"{parsed['emoji']} بازی ساخته شد!\n\n"
        f"👤 سازنده: {display_name(user.id)}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "👥 منتظر بازیکن دوم...",
        reply_markup=kb
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]
    user = q.from_user

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True
        )

        return

    if user.id == game["creator"]:

        await q.answer(
            "❌ نمی‌توانی وارد بازی خودت شوی.",
            show_alert=True
        )

        return

    register(user)

    old = user_game(user.id)

    if old:

        await q.answer(
            "❌ شما یک بازی فعال دیگر دارید.",
            show_alert=True
        )

        return

    amount = float(game["amount"])

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

            if not current or current["status"] != "waiting":

                con.rollback()

                await q.answer(
                    "❌ بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                user.id,
            )).fetchone()

            if not row:

                con.rollback()

                await q.answer(
                    "❌ حساب پیدا نشد.",
                    show_alert=True
                )

                return

            current_balance = float(row["balance"])

            if current_balance < amount:

                con.rollback()

                await q.answer(
                    f"❌ موجودی کافی نیست.\n"
                    f"نیاز: {money(amount)} TRX",
                    show_alert=True
                )

                return

            cur = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if cur.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ ورود انجام نشد.",
                    show_alert=True
                )

                return

            cur = con.execute("""
            UPDATE games
            SET
                opponent=?,
                status='playing',
                mode='friend'
            WHERE id=?
            AND status='waiting'
            """, (
                user.id,
                game_id
            ))

            if cur.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "join_game error"
            )

            await q.answer(
                "❌ خطا در ورود.",
                show_alert=True
            )

            return

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        "🎮 بازی شروع شد!\n\n"
        f"👤 سازنده: {display_name(game['creator'])}\n"
        f"👤 حریف: {display_name(user.id)}\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط هر نفر: {money(amount)} TRX\n\n"
        f"👤 {display_name(game['creator'])} "
        f"اول پرتاب کند."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]
    user = q.from_user

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if user.id != game["creator"]:

        await q.answer(
            "❌ فقط سازنده می‌تواند با ربات بازی کند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

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

            if not current or current["status"] != "waiting":

                con.rollback()

                await q.answer(
                    "❌ بازی دیگر قابل شروع نیست.",
                    show_alert=True
                )

                return

            cur = con.execute("""
            UPDATE games
            SET
                opponent=0,
                mode='robot',
                status='playing'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            if cur.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ شروع نشد.",
                    show_alert=True
                )

                return

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "robot_game error"
            )

            await q.answer(
                "❌ خطا در شروع بازی.",
                show_alert=True
            )

            return

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"👤 بازیکن: {display_name(user.id)}\n"
        "🤖 حریف: ربات\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 تعداد دور: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"👤 اول شما هر {game['rounds']} پرتاب "
        "را کامل انجام بده.\n\n"
        "بعد از آخرین پرتاب، ربات شروع می‌کند."
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if q.from_user.id != game["creator"]:

        await q.answer(
            "❌ فقط سازنده می‌تواند لغو کند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده و قابل لغو نیست.",
            show_alert=True
        )

        return

    amount = float(game["amount"])

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

            if not current or current["status"] != "waiting":

                con.rollback()

                await q.answer(
                    "❌ بازی قبلاً تغییر کرده.",
                    show_alert=True
                )

                return

            cur = con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            if cur.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ لغو انجام نشد.",
                    show_alert=True
                )

                return

            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                current["creator"]
            ))

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "cancel_game error"
            )

            await q.answer(
                "❌ خطا در لغو.",
                show_alert=True
            )

            return

    await q.answer(
        "✅ لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(amount)} TRX برگشت داده شد."
    )


# =========================================================
# ROBOT THROW
# =========================================================

async def robot_throw(game_id, context):

    game = get_game(game_id)

    if not game:
        return None

    if game["status"] != "playing":
        return None

    if game["mode"] != "robot":
        return None

    if game["opponent_round"] >= game["rounds"]:
        return None

    try:

        sent = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        value = int(sent.dice.value)

    except Exception:

        log.exception(
            "robot throw error"
        )

        return None

    if not valid_game_value(
        game["emoji"],
        value
    ):
        return None

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

            if not current:
                con.rollback()
                return None

            if current["status"] != "playing":
                con.rollback()
                return None

            if current["mode"] != "robot":
                con.rollback()
                return None

            if current["opponent_round"] >= current["rounds"]:
                con.rollback()
                return None

            cur = con.execute("""
            UPDATE games
            SET
                opponent_round=opponent_round+1,
                opponent_score=opponent_score+?
            WHERE id=?
            AND status='playing'
            AND mode='robot'
            AND opponent_round < rounds
            """, (
                value,
                game_id
            ))

            if cur.rowcount != 1:

                con.rollback()
                return None

            con.commit()

            return value

        except Exception:

            con.rollback()

            log.exception(
                "robot score error"
            )

            return None


# =========================================================
# FINISH GAME
# =========================================================

async def finish(game_id, context):

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

                con.rollback()
                return

            if game["status"] != "playing":

                con.rollback()
                return

            if game["creator_round"] < game["rounds"]:

                con.rollback()
                return

            if game["opponent_round"] < game["rounds"]:

                con.rollback()
                return

            creator_score = int(
                game["creator_score"]
            )

            opponent_score = int(
                game["opponent_score"]
            )

            amount = float(
                game["amount"]
            )

            cur = con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
            AND status='playing'
            """, (
                game_id,
            ))

            if cur.rowcount != 1:

                con.rollback()
                return

            creator = game["creator"]

            if game["mode"] == "robot":

                # در بازی با ربات:
                # اگر کاربر برد، مبلغ خودش + مبلغ ربات
                # به موجودی کاربر برمی‌گردد.
                if creator_score > opponent_score:

                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        creator
                    ))

                    result_text = (
                        "🏆 شما برنده شدید!\n"
                        f"💰 دریافتی: "
                        f"{money(payout)} TRX"
                    )

                elif creator_score < opponent_score:

                    result_text = (
                        "🤖 ربات برنده شد.\n"
                        f"💸 مبلغ بازی: "
                        f"{money(amount)} TRX"
                    )

                else:

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        creator
                    ))

                    result_text = (
                        "🤝 بازی مساوی شد.\n"
                        f"💰 {money(amount)} TRX "
                        "برگشت داده شد."
                    )

            else:

                opponent = game["opponent"]

                if creator_score > opponent_score:

                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        creator
                    ))

                    result_text = (
                        f"🏆 برنده: "
                        f"{display_name(creator)}\n"
                        f"💰 برد: {money(payout)} TRX"
                    )

                elif opponent_score > creator_score:

                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        opponent
                    ))

                    result_text = (
                        f"🏆 برنده: "
                        f"{display_name(opponent)}\n"
                        f"💰 برد: {money(payout)} TRX"
                    )

                else:

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        creator
                    ))

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        opponent
                    ))

                    result_text = (
                        "🤝 مساوی!\n"
                        f"💰 {money(amount)} TRX "
                        "به هر دو نفر برگشت."
                    )

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "finish error"
            )

            return

    creator_name = display_name(
        game["creator"]
    )

    if game["mode"] == "robot":
        opponent_name = "🤖 ربات"
    else:
        opponent_name = display_name(
            game["opponent"]
        )

    text = (
        f"{game['emoji']} نتیجه بازی\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"🤖/👤 حریف: {opponent_name}\n\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "📊 امتیاز نهایی:\n"
        f"👤 {creator_name}: "
        f"{creator_score}\n"
        f"👤 {opponent_name}: "
        f"{opponent_score}\n\n"
        f"{result_text}"
    )

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=text
        )

    except Exception:

        log.exception(
            "result message error"
        )


# =========================================================
# DICE/GAME HANDLER
# =========================================================

async def dice_handler(update, context):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    dice = msg.dice

    if not dice:
        return

    emoji = dice.emoji

    if emoji not in GAMES.values():
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    try:
        value = int(dice.value)
    except Exception:
        return

    if not valid_game_value(
        emoji,
        value
    ):
        return

    # =====================================================
    # پیدا کردن بازی مخصوص همین کاربر
    # =====================================================

    with closing(db()) as con:

        game = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND emoji=?
        AND status='playing'
        AND (
            creator=?
            OR opponent=?
        )
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            msg.chat.id,
            emoji,
            user.id,
            user.id
        )).fetchone()

    if not game:
        return

    # =====================================================
    # ROBOT
    # =====================================================

    if game["mode"] == "robot":

        if user.id != game["creator"]:
            return

        # کاربر تمام کرده
        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده؛ "
                "نوبت ربات است."
            )

            return

        # ثبت امن پرتاب کاربر
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
                    game["id"],
                )).fetchone()

                if not current:
                    con.rollback()
                    return

                if current["status"] != "playing":
                    con.rollback()
                    return

                if current["mode"] != "robot":
                    con.rollback()
                    return

                if current["creator"] != user.id:
                    con.rollback()
                    return

                if current["creator_round"] >= current["rounds"]:

                    con.rollback()

                    await msg.reply_text(
                        "⏳ پرتاب‌های شما تمام شده."
                    )

                    return

                cur = con.execute("""
                UPDATE games
                SET
                    creator_round=creator_round+1,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                AND mode='robot'
                AND creator_round < rounds
                """, (
                    value,
                    game["id"]
                ))

                if cur.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "user robot throw error"
                )

                return

        current = get_game(
            game["id"]
        )

        if not current:
            return

        # سریع اعلام ثبت
        await msg.reply_text(
            f"👤 {display_name(user.id)}: "
            f"{value}\n"
            f"📊 پرتاب: "
            f"{current['creator_round']}/"
            f"{current['rounds']}"
        )

        # هنوز کاربر تمام نکرده
        if current["creator_round"] < current["rounds"]:

            return

        # =================================================
        # کاربر تمام کرد -> ربات شروع
        # =================================================

        await msg.reply_text(
            "🤖 تمام پرتاب‌های شما ثبت شد.\n"
            "🤖 حالا نوبت ربات است..."
        )

        # ربات تمام پرتاب‌ها را پشت سر هم
        while True:

            current = get_game(
                game["id"]
            )

            if not current:
                return

            if current["status"] != "playing":
                return

            if current["opponent_round"] >= current["rounds"]:
                break

            robot_value = await robot_throw(
                game["id"],
                context
            )

            if robot_value is None:

                await msg.reply_text(
                    "❌ در پرتاب ربات خطایی رخ داد."
                )

                return

            await asyncio.sleep(0.25)

            await msg.reply_text(
                f"🤖 ربات: {robot_value}"
            )

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

            await finish(
                current["id"],
                context
            )

        return

    # =====================================================
    # FRIEND MODE
    # =====================================================

    if game["mode"] != "friend":
        return

    # =====================================================
    # CREATOR
    # =====================================================

    if user.id == game["creator"]:

        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        # اگر سازنده یک پرتاب جلوتر است
        if game["creator_round"] > game["opponent_round"]:

            await msg.reply_text(
                "⏳ هنوز نوبت حریف است."
            )

            return

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
                    game["id"],
                )).fetchone()

                if not current:
                    con.rollback()
                    return

                if current["status"] != "playing":
                    con.rollback()
                    return

                if current["creator_round"] >= current["rounds"]:
                    con.rollback()
                    return

                if (
                    current["creator_round"]
                    >
                    current["opponent_round"]
                ):

                    con.rollback()
                    return

                cur = con.execute("""
                UPDATE games
                SET
                    creator_round=creator_round+1,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                AND creator_round < rounds
                AND creator_round=opponent_round
                """, (
                    value,
                    game["id"]
                ))

                if cur.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend creator error"
                )

                return

        await msg.reply_text(
            f"👤 {display_name(game['creator'])}: "
            f"{value}\n"
            "⏳ نوبت حریف."
        )

    # =====================================================
    # OPPONENT
    # =====================================================

    elif user.id == game["opponent"]:

        if game["opponent_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        if (
            game["creator_round"]
            <=
            game["opponent_round"]
        ):

            await msg.reply_text(
                "⏳ هنوز نوبت شما نیست."
            )

            return

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
                    game["id"],
                )).fetchone()

                if not current:
                    con.rollback()
                    return

                if current["status"] != "playing":
                    con.rollback()
                    return

                if current["opponent_round"] >= current["rounds"]:
                    con.rollback()
                    return

                if (
                    current["creator_round"]
                    <=
                    current["opponent_round"]
                ):

                    con.rollback()
                    return

                cur = con.execute("""
                UPDATE games
                SET
                    opponent_round=opponent_round+1,
                    opponent_score=opponent_score+?
                WHERE id=?
                AND status='playing'
                AND opponent_round < rounds
                AND creator_round > opponent_round
                """, (
                    value,
                    game["id"]
                ))

                if cur.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend opponent error"
                )

                return

        await msg.reply_text(
            f"👤 {display_name(game['opponent'])}: "
            f"{value}\n"
            "⏳ نوبت سازنده."
        )

    else:

        return

    # =====================================================
    # FINISH FRIEND
    # =====================================================

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

        await finish(
            current["id"],
            context
        )


# =========================================================
# BALANCE BUTTON
# =========================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        f"💰 موجودی {q.from_user.full_name}\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# BALANCE TEXT
# =========================================================

async def balance_text(update, context):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    await msg.reply_text(
        f"💰 موجودی {user.full_name}\n\n"
        f"{money(balance(user.id))} TRX"
    )


# =========================================================
# TRANSFER BUTTON
# =========================================================

async def transfer_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام گیرنده Reply کن و بنویس:\n\n"
        "انتقال 10\n\n"
        "مثال:\n"
        "انتقال 0.1"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_handler(update, context):

    msg = update.message

    if not msg:
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    user = update.effective_user

    if not user:
        return

    text = digits(
        msg.text.strip()
    )

    match = re.fullmatch(
        r"^انتقال\s+"
        r"(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 0.1"
        )

        return

    try:

        amount = float(
            match.group(1)
        )

    except Exception:

        await msg.reply_text(
            "❌ مبلغ اشتباه است."
        )

        return

    if amount <= 0 or amount > MAX_AMOUNT:

        await msg.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    receiver = msg.reply_to_message.from_user

    if not receiver:
        return

    if receiver.is_bot:

        await msg.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    if user.id == receiver.id:

        await msg.reply_text(
            "❌ انتقال به خودت ممکن نیست."
        )

        return

    register(user)
    register(receiver)

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            sender = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                user.id,
            )).fetchone()

            target = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
            """, (
                receiver.id,
            )).fetchone()

            if not sender or not target:

                con.rollback()

                await msg.reply_text(
                    "❌ حساب پیدا نشد."
                )

                return

            sender_balance = float(
                sender["balance"]
            )

            if sender_balance < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{money(sender_balance)} TRX\n"
                    f"💸 مبلغ: "
                    f"{money(amount)} TRX"
                )

                return

            cur = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if cur.rowcount != 1:

                con.rollback()

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

            cur = con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id
            ))

            if cur.rowcount != 1:

                con.rollback()

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "transfer error"
            )

            await msg.reply_text(
                "❌ خطا در انتقال."
            )

            return

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {receiver.full_name}\n"
        f"💰 مقدار: {money(amount)} TRX\n"
        f"💳 موجودی شما: "
        f"{money(balance(user.id))} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "در این نسخه برداشت بلاکچینی وجود ندارد.\n"
        "موجودی فقط TRX داخلی بات است."
    )


# =========================================================
# HELP
# =========================================================

async def help_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی:\n"
        "3 تاس 100\n"
        "3 دارت 100\n"
        "3 بسکتبال 100\n"
        "3 بولینگ 100\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام گیرنده Reply کن:\n"
        "انتقال 10\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر تمام پرتاب‌ها را می‌اندازد.\n"
        "بعد ربات تمام پرتاب‌ها را می‌اندازد.\n"
        "بعد نتیجه اعلام می‌شود."
    )


# =========================================================
# ADMIN KEYBOARD
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ شارژ موجودی",
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
                "🔙 برگشت",
                callback_data="home"
            )
        ]
    ])


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:

        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    await q.answer()

    await q.message.reply_text(
        "👑 پنل مدیریت\n\n"
        "فقط از گزینه‌های زیر استفاده کن.",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN ADD
# =========================================================

async def admin_add(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer()
        return

    await q.answer()

    context.user_data["admin_action"] = "add"

    await q.message.reply_text(
        "➕ شارژ موجودی\n\n"
        "در PV ربات بنویس:\n\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 100"
    )


# =========================================================
# ADMIN REMOVE
# =========================================================

async def admin_remove(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer()
        return

    await q.answer()

    context.user_data["admin_action"] = "remove"

    await q.message.reply_text(
        "➖ کسر موجودی\n\n"
        "در PV ربات بنویس:\n\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 100"
    )


# =========================================================
# ADMIN BALANCE
# =========================================================

async def admin_balance(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer()
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "💰 موجودی کاربر\n\n"
        "آیدی عددی کاربر را بفرست."
    )


# =========================================================
# ADMIN STATS
# =========================================================

async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer()
        return

    await q.answer()

    with closing(db()) as con:

        users = con.execute("""
        SELECT COUNT(*) AS c
        FROM users
        """).fetchone()["c"]

        games_count = con.execute("""
        SELECT COUNT(*) AS c
        FROM games
        """).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) AS c
        FROM games
        WHERE status IN ('waiting', 'playing')
        """).fetchone()["c"]

        total_balance = con.execute("""
        SELECT COALESCE(SUM(balance), 0) AS b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 کل بازی‌ها: {games_count}\n"
        f"⏳ بازی فعال: {active}\n"
        f"💰 مجموع موجودی: "
        f"{money(total_balance)} TRX"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update, context):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user:
        return

    if user.id != OWNER_ID:
        return

    # خیلی مهم:
    # پنل مدیریت فقط در PV کار می‌کند.
    # بنابراین پیام‌های گپ بازی دیگر وارد این بخش نمی‌شوند.
    if msg.chat.type != ChatType.PRIVATE:
        return

    action = context.user_data.get(
        "admin_action"
    )

    if not action:
        return

    text = digits(
        msg.text.strip()
    )

    # =====================================================
    # موجودی کاربر
    # =====================================================

    if action == "balance":

        if not re.fullmatch(
            r"\d{1,20}",
            text
        ):

            await msg.reply_text(
                "❌ فقط آیدی عددی را بفرست."
            )

            return

        target = int(text)

        await msg.reply_text(
            "💰 موجودی کاربر\n\n"
            f"🆔 {target}\n"
            f"💳 {money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return

    # =====================================================
    # افزایش / کاهش
    # =====================================================

    match = re.fullmatch(
        r"^(\d{1,20})\s+"
        r"(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:

        await msg.reply_text(
            "❌ فرمت صحیح:\n\n"
            "آیدی مبلغ\n\n"
            "مثال:\n"
            "123456789 100"
        )

        return

    target = int(
        match.group(1)
    )

    amount = float(
        match.group(2)
    )

    if amount <= 0 or amount > MAX_AMOUNT:

        await msg.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    if not get_user(target):

        await msg.reply_text(
            "❌ این کاربر هنوز در دیتابیس نیست."
        )

        return

    if action == "add":

        ok = add_balance(
            target,
            amount
        )

    elif action == "remove":

        ok = add_balance(
            target,
            -amount
        )

    else:

        ok = False

    if ok:

        await msg.reply_text(
            "✅ عملیات انجام شد.\n\n"
            f"🆔 کاربر: {target}\n"
            f"💳 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await msg.reply_text(
            "❌ عملیات انجام نشد.\n"
            "برای کسر، موجودی کاربر کافی نیست."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


# =========================================================
# ADD BALANCE
# =========================================================

def add_balance(user_id, amount):

    try:
        amount = float(amount)
    except Exception:
        return False

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

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

            old_balance = float(
                row["balance"]
            )

            new_balance = old_balance + amount

            # ضد موجودی منفی
            if new_balance < 0:

                con.rollback()
                return False

            cur = con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                round(new_balance, 8),
                user_id
            ))

            if cur.rowcount != 1:

                con.rollback()
                return False

            con.commit()

            return True

        except Exception:

            con.rollback()

            log.exception(
                "add_balance error"
            )

            return False


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback(update, context):

    q = update.callback_query

    if not q:
        return

    data = q.data or ""

    # =====================================================
    # MEMBERSHIP
    # =====================================================

    if data == "membership":

        ok = await member_ok(
            context.bot,
            q.from_user.id
        )

        await q.answer(
            "✅ عضو هستی."
            if ok
            else
            "❌ هنوز عضو نیستی.",
            show_alert=True
        )

        return

    # =====================================================
    # MAIN
    # =====================================================

    if data == "games":

        await games(update, context)
        return

    if data == "examples":

        await examples(update, context)
        return

    if data == "friends":

        await friends(update, context)
        return

    if data == "robot_help":

        await robot_help(update, context)
        return

    if data == "balance":

        await balance_button(update, context)
        return

    if data == "transfer":

        await transfer_button(update, context)
        return

    if data == "withdraw":

        await withdraw(update, context)
        return

    if data == "help":

        await help_button(update, context)
        return

    if data == "admin":

        await admin(update, context)
        return

    # =====================================================
    # ADMIN
    # =====================================================

    if data == "admin_add":

        await admin_add(update, context)
        return

    if data == "admin_remove":

        await admin_remove(update, context)
        return

    if data == "admin_balance":

        await admin_balance(update, context)
        return

    if data == "admin_stats":

        await admin_stats(update, context)
        return

    # =====================================================
    # HOME
    # =====================================================

    if data == "home":

        await q.answer()

        await q.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=keyboard(
                q.from_user.id
            )
        )

        return

    # =====================================================
    # GAME CALLBACKS
    # =====================================================

    if data.startswith("join:"):

        await join_game(update, context)
        return

    if data.startswith("robot:"):

        await robot_game(update, context)
        return

    if data.startswith("cancel:"):

        await cancel_game(update, context)
        return

    # هیچ callback دیگری اجرا نشود
    return


# =========================================================
# TEXT ROUTER
# فقط یک مسیر برای هر پیام
# =========================================================

async def text_router(update, context):

    msg = update.message

    if not msg or not msg.text:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    text = digits(
        msg.text.strip()
    )

    # ضد پیام خراب
    if not text:
        return

    if len(text) > 200:
        return

    # =====================================================
    # 1) پنل مدیریت
    # فقط PV مالک
    # =====================================================

    if (
        user.id == OWNER_ID
        and
        msg.chat.type == ChatType.PRIVATE
        and
        context.user_data.get("admin_action")
    ):

        await admin_text(
            update,
            context
        )

        return

    # =====================================================
    # 2) موجودی
    # فقط یک دستور
    # =====================================================

    if re.fullmatch(
        r"موجودی",
        text
    ):

        await balance_text(
            update,
            context
        )

        return

    # =====================================================
    # 3) انتقال
    # =====================================================

    if re.fullmatch(
        r"انتقال\s+\d{1,8}(?:\.\d{1,8})?",
        text
    ):

        await transfer_handler(
            update,
            context
        )

        return

    # اگر انتقال ناقص بود، فقط همان پیام
    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # =====================================================
    # 4) بازی
    # =====================================================

    parsed = parse_game(text)

    if parsed:

        await create_game(
            update,
            context
        )

        return

    # =====================================================
    # 5) هیچ دستور دیگری اجرا نشود
    # =====================================================

    return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    log.error(
        "BOT ERROR: %s",
        context.error,
        exc_info=context.error
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
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # =====================================================
    # COMMAND
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # =====================================================
    # CALLBACK
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # =====================================================
    # TELEGRAM DICE
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router
        )
    )

    # =====================================================
    # ERROR
    # =====================================================

    app.add_error_handler(
        error_handler
    )

    log.info(
        "BET_BT STARTED"
    )

    # =====================================================
    # POLLING
    # =====================================================

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
