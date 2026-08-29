import os
import re
import sqlite3
import secrets
import logging
import asyncio
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

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = 8552447077

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB = "bot.sqlite3"

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
            amount REAL NOT NULL,

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
        """, (user_id,)).fetchone()


def balance(user_id):
    row = get_user(user_id)

    if not row:
        return 0.0

    return max(0.0, float(row["balance"]))


def money(value):
    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# =========================================================
# BALANCE OPERATIONS
# =========================================================

def change_balance(user_id, amount):
    """
    تغییر اتمیک موجودی.
    amount مثبت = افزایش
    amount منفی = کاهش
    """

    amount = float(amount)

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                con.rollback()
                return False

            old = float(row["balance"])
            new = old + amount

            if new < 0:
                con.rollback()
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                round(new, 8),
                user_id
            ))

            con.commit()
            return True

        except Exception:
            con.rollback()
            log.exception("change_balance error")
            return False


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
    if user_id == 0:
        return "🤖 ربات"

    row = get_user(user_id)

    if not row:
        return f"کاربر {user_id}"

    name = row["name"] or ""
    username = row["username"] or ""

    if name:
        return name

    if username:
        return "@" + username

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

    match = re.fullmatch(pattern, text)

    if not match:
        return None

    try:
        rounds = int(match.group(1))
        amount = float(match.group(3))
    except Exception:
        return None

    if rounds < 1 or rounds > 100:
        return None

    if amount <= 0 or amount > 1000000:
        return None

    game = match.group(2)

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
        """, (game_id,)).fetchone()


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
        # اگر کانال در دسترس نبود، ربات از کار نیفتد.
        return True


async def membership(update, context):

    user = update.effective_user

    if not user:
        return False

    if await member_ok(context.bot, user.id):
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

def main_keyboard(user_id):

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

    if not await membership(update, context):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از دکمه‌های زیر استفاده کن.",
        reply_markup=main_keyboard(user.id)
    )


# =========================================================
# GAMES MENU
# =========================================================

async def show_games(update, context):

    q = update.callback_query

    await q.answer()

    keyboard = InlineKeyboardMarkup([
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
                "🎯 مثال بازی",
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
        "برای ساخت بازی در گپ بنویس:\n\n"
        "3 تاس 1\n"
        "3 دارت 1\n"
        "3 بسکتبال 1\n"
        "3 بولینگ 1\n\n"
        "بعد می‌توانی بازی را با دوست یا ربات انجام بدهی.",
        reply_markup=keyboard
    )


# =========================================================
# EXAMPLES
# =========================================================

async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال‌ها:\n\n"
        "3 تاس 1\n"
        "3 دارت 1\n"
        "3 بسکتبال 1\n"
        "3 بولینگ 1\n\n"
        "عدد اول = تعداد پرتاب\n"
        "عدد آخر = شرط به TRX داخلی\n\n"
        "مثلاً:\n"
        "3 تاس 1\n\n"
        "یعنی ۳ پرتاب و شرط ۱ TRX مجازی."
    )


# =========================================================
# FRIEND HELP
# =========================================================

async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "3 تاس 1\n\n"
        "بعد بازیکن دوم روی «ورود به بازی» می‌زند.\n\n"
        "نوبت‌ها:\n"
        "👤 بازیکن اول\n"
        "👤 بازیکن دوم\n"
        "👤 بازیکن اول\n"
        "👤 بازیکن دوم\n\n"
        "تا تعداد پرتاب مشخص‌شده کامل شود."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "هر ۴ بازی همین منطق را دارند:\n\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "اول کاربر تمام پرتاب‌های خودش را انجام می‌دهد.\n"
        "مثلاً اگر بازی 3 دور باشد:\n\n"
        "1️⃣ پرتاب کاربر\n"
        "2️⃣ پرتاب کاربر\n"
        "3️⃣ پرتاب کاربر\n\n"
        "بعد ربات:\n\n"
        "🤖 پرتاب اول\n"
        "🤖 پرتاب دوم\n"
        "🤖 پرتاب سوم\n\n"
        "و بلافاصله نتیجه نمایش داده می‌شود."
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

    parsed = parse_game(msg.text)

    if not parsed:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    if not await membership(update, context):
        return

    # ضد بازی همزمان
    old_game = user_game(user.id)

    if old_game:
        await msg.reply_text(
            "❌ شما یک بازی فعال دارید.\n\n"
            "اول همان بازی را تمام کنید یا لغو کنید."
        )
        return

    amount = parsed["amount"]
    game_id = secrets.token_hex(16)

    # =====================================================
    # کسر شرط و ساخت بازی در یک تراکنش
    # =====================================================

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user.id,)).fetchone()

            if not row:
                con.rollback()

                await msg.reply_text(
                    "❌ حساب شما پیدا نشد."
                )
                return

            current_balance = float(row["balance"])

            if current_balance < amount:
                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{money(current_balance)} TRX\n"
                    f"🎯 شرط: "
                    f"{money(amount)} TRX"
                )
                return

            # کسر شرط
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
                    "❌ موجودی تغییر کرده؛ دوباره امتحان کن."
                )
                return

            # ثبت بازی
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
                ?, ?, ?, NULL,
                ?, ?, ?, ?,
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
            log.exception("create_game error")

            await msg.reply_text(
                "❌ خطا در ساخت بازی."
            )
            return

    keyboard = InlineKeyboardMarkup([
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
                "❌ لغو بازی",
                callback_data=f"cancel:{game_id}"
            )
        ]
    ])

    await msg.reply_text(
        f"{parsed['emoji']} بازی ساخته شد!\n\n"
        f"👤 سازنده: {display_name(user.id)}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 تعداد پرتاب: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "یکی از گزینه‌ها را انتخاب کن:",
        reply_markup=keyboard
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(update, context):

    q = update.callback_query

    try:
        game_id = q.data.split(":", 1)[1]
    except Exception:
        await q.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    user = q.from_user

    if not user:
        await q.answer()
        return

    register(user)

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not game:
                con.rollback()

                await q.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )
                return

            if game["status"] != "waiting":
                con.rollback()

                await q.answer(
                    "❌ این بازی دیگر قابل ورود نیست.",
                    show_alert=True
                )
                return

            if user.id == game["creator"]:
                con.rollback()

                await q.answer(
                    "❌ سازنده نمی‌تواند وارد بازی خودش شود.",
                    show_alert=True
                )
                return

            old = con.execute("""
            SELECT id
            FROM games
            WHERE status IN ('waiting','playing')
            AND (
                creator=?
                OR opponent=?
            )
            LIMIT 1
            """, (
                user.id,
                user.id
            )).fetchone()

            if old:
                con.rollback()

                await q.answer(
                    "❌ شما یک بازی فعال دیگر دارید.",
                    show_alert=True
                )
                return

            amount = float(game["amount"])

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user.id,)).fetchone()

            if not row:
                con.rollback()

                await q.answer(
                    "❌ حساب شما پیدا نشد.",
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

            # کسر شرط حریف
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
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )
                return

            # ورود حریف
            cur = con.execute("""
            UPDATE games
            SET
                opponent=?,
                mode='friend',
                status='playing'
            WHERE id=?
            AND status='waiting'
            AND opponent IS NULL
            """, (
                user.id,
                game_id
            ))

            if cur.rowcount != 1:
                con.rollback()

                # چون کسر هم داخل همین تراکنش بود،
                # برگشت داده می‌شود.
                await q.answer(
                    "❌ ورود انجام نشد؛ بازی تغییر کرده.",
                    show_alert=True
                )
                return

            con.commit()

        except Exception:
            con.rollback()
            log.exception("join_game error")

            await q.answer(
                "❌ خطا در ورود.",
                show_alert=True
            )
            return

    await q.answer("✅ وارد بازی شدی.")

    await q.message.reply_text(
        "🎮 بازی شروع شد!\n\n"
        f"👤 سازنده: {display_name(game['creator'])}\n"
        f"👤 حریف: {display_name(user.id)}\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 پرتاب‌ها: {game['rounds']}\n"
        f"💰 شرط هر نفر: {money(game['amount'])} TRX\n\n"
        f"👤 {display_name(game['creator'])} "
        f"اولین پرتاب را بفرست."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(update, context):

    q = update.callback_query

    try:
        game_id = q.data.split(":", 1)[1]
    except Exception:
        await q.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    user = q.from_user

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not game:
                con.rollback()

                await q.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )
                return

            if user.id != game["creator"]:
                con.rollback()

                await q.answer(
                    "❌ فقط سازنده می‌تواند با ربات بازی کند.",
                    show_alert=True
                )
                return

            if game["status"] != "waiting":
                con.rollback()

                await q.answer(
                    "❌ این بازی دیگر قابل شروع نیست.",
                    show_alert=True
                )
                return

            # تبدیل بازی به robot
            cur = con.execute("""
            UPDATE games
            SET
                opponent=0,
                mode='robot',
                status='playing'
            WHERE id=?
            AND status='waiting'
            """, (game_id,))

            if cur.rowcount != 1:
                con.rollback()

                await q.answer(
                    "❌ بازی تغییر کرده.",
                    show_alert=True
                )
                return

            con.commit()

        except Exception:
            con.rollback()
            log.exception("robot_game error")

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
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"👤 اول تمام {game['rounds']} پرتاب خودت را انجام بده.\n"
        "بعد از پرتاب آخر، ربات خودش همه پرتاب‌ها را انجام می‌دهد."
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_game(update, context):

    q = update.callback_query

    try:
        game_id = q.data.split(":", 1)[1]
    except Exception:
        await q.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not game:
                con.rollback()

                await q.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )
                return

            if q.from_user.id != game["creator"]:
                con.rollback()

                await q.answer(
                    "❌ فقط سازنده می‌تواند لغو کند.",
                    show_alert=True
                )
                return

            if game["status"] != "waiting":
                con.rollback()

                await q.answer(
                    "❌ بازی شروع شده و قابل لغو نیست.",
                    show_alert=True
                )
                return

            cur = con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (game_id,))

            if cur.rowcount != 1:
                con.rollback()

                await q.answer(
                    "❌ لغو انجام نشد.",
                    show_alert=True
                )
                return

            # برگشت شرط
            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                float(game["amount"]),
                game["creator"]
            ))

            con.commit()

        except Exception:
            con.rollback()
            log.exception("cancel error")

            await q.answer(
                "❌ خطا در لغو.",
                show_alert=True
            )
            return

    await q.answer("✅ بازی لغو شد.")

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(game['amount'])} TRX "
        "به موجودی سازنده برگشت."
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
        result = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        value = int(result.dice.value)

    except Exception:
        log.exception("send robot dice error")
        return None

    if not valid_game_value(
        game["emoji"],
        value
    ):
        return None

    # ثبت امتیاز فقط اگر هنوز نوبت ربات باقی مانده
    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            current = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

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
            log.exception("robot score error")
            return None


# =========================================================
# FINISH GAME
# =========================================================

async def finish_game(game_id, context):

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not game:
                con.rollback()
                return False

            if game["status"] != "playing":
                con.rollback()
                return False

            # هر دو باید تمام کرده باشند
            if (
                game["creator_round"] < game["rounds"]
                or
                game["opponent_round"] < game["rounds"]
            ):
                con.rollback()
                return False

            creator_score = int(game["creator_score"])
            opponent_score = int(game["opponent_score"])
            amount = float(game["amount"])

            # اول وضعیت را finished می‌کنیم
            # تا تسویه دوباره اتفاق نیفتد.
            cur = con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
            AND status='playing'
            """, (game_id,))

            if cur.rowcount != 1:
                con.rollback()
                return False

            creator_name = display_name(
                game["creator"]
            )

            if game["mode"] == "robot":
                opponent_name = "🤖 ربات"
            else:
                opponent_name = display_name(
                    game["opponent"]
                )

            # =================================================
            # CREATOR WIN
            # =================================================

            if creator_score > opponent_score:

                payout = amount * 2

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    payout,
                    game["creator"]
                ))

                result_text = (
                    f"🏆 برنده: {creator_name}\n"
                    f"💰 جایزه: {money(payout)} TRX"
                )

            # =================================================
            # OPPONENT WIN
            # =================================================

            elif opponent_score > creator_score:

                payout = amount * 2

                if game["mode"] == "friend":

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        game["opponent"]
                    ))

                    result_text = (
                        f"🏆 برنده: {opponent_name}\n"
                        f"💰 جایزه: {money(payout)} TRX"
                    )

                else:
                    # ربات موجودی ندارد.
                    # در این حالت شرط بازیکن پرداخت نمی‌شود.
                    result_text = (
                        "🤖 ربات برنده شد.\n"
                        f"💰 جایزه: {money(payout)} TRX"
                    )

            # =================================================
            # DRAW
            # =================================================

            else:

                # مساوی:
                # پول هر کاربر برمی‌گردد.
                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount,
                    game["creator"]
                ))

                if game["mode"] == "friend":

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        game["opponent"]
                    ))

                result_text = (
                    "🤝 بازی مساوی شد.\n"
                    f"💰 {money(amount)} TRX "
                    "به بازیکن برگشت."
                )

            con.commit()

        except Exception:
            con.rollback()
            log.exception("finish_game error")
            return False

    # =====================================================
    # RESULT MESSAGE
    # =====================================================

    text = (
        f"{game['emoji']} نتیجه نهایی\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"👤 حریف: {opponent_name}\n\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "📊 امتیاز نهایی:\n\n"
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
        log.exception("result send error")

    return True


# =========================================================
# ROBOT GAME AFTER USER FINISHES
# =========================================================

async def run_robot_turn(
    game_id,
    context
):

    game = get_game(game_id)

    if not game:
        return

    if game["status"] != "playing":
        return

    if game["mode"] != "robot":
        return

    if game["creator_round"] < game["rounds"]:
        return

    # =====================================================
    # ربات تمام پرتاب‌ها را انجام می‌دهد
    # =====================================================

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            "🤖 نوبت ربات شد!\n\n"
            "ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
        )
    )

    while True:

        current = get_game(game_id)

        if not current:
            return

        if current["status"] != "playing":
            return

        if current["opponent_round"] >= current["rounds"]:
            break

        value = await robot_throw(
            game_id,
            context
        )

        if value is None:
            # اگر ارسال یک پرتاب خطا خورد،
            # بازی را خراب نکن؛ کمی صبر و دوباره تلاش.
            await asyncio.sleep(1)

            current = get_game(game_id)

            if not current:
                return

            if current["opponent_round"] >= current["rounds"]:
                break

            # تلاش دوم
            value = await robot_throw(
                game_id,
                context
            )

            if value is None:
                await context.bot.send_message(
                    chat_id=current["chat_id"],
                    text="❌ ارسال پرتاب ربات با خطا مواجه شد. بازی متوقف شد."
                )
                return

        await asyncio.sleep(0.7)

    current = get_game(game_id)

    if not current:
        return

    if (
        current["creator_round"] >= current["rounds"]
        and
        current["opponent_round"] >= current["rounds"]
    ):
        await finish_game(
            game_id,
            context
        )


# =========================================================
# DICE HANDLER
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

    value = int(dice.value)

    if not valid_game_value(
        emoji,
        value
    ):
        return

    # =====================================================
    # پیدا کردن بازی مربوط به همین کاربر
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
        # تاس عادی خارج از بازی را نادیده می‌گیریم.
        return

    # =====================================================
    # ROBOT MODE
    # =====================================================

    if game["mode"] == "robot":

        # فقط سازنده می‌تواند تاس بیندازد.
        if user.id != game["creator"]:
            return

        # اگر کاربر تمام کرده
        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده؛ "
                "الان نوبت ربات است."
            )
            return

        # =================================================
        # ثبت پرتاب کاربر
        # =================================================

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game["id"],)).fetchone()

                if not current:
                    con.rollback()
                    return

                if current["status"] != "playing":
                    con.rollback()
                    return

                if current["mode"] != "robot":
                    con.rollback()
                    return

                if current["creator_round"] >= current["rounds"]:
                    con.rollback()
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
                log.exception("user robot throw error")
                return

        current = get_game(game["id"])

        if not current:
            return

        # =================================================
        # نمایش همان پرتاب
        # =================================================

        await msg.reply_text(
            f"👤 {display_name(user.id)}: {value}\n\n"
            f"📊 پرتاب شما: "
            f"{current['creator_round']}/"
            f"{current['rounds']}"
        )

        # =================================================
        # هنوز کاربر تمام نکرده
        # =================================================

        if current["creator_round"] < current["rounds"]:

            await msg.reply_text(
                "⏳ هنوز نوبت شماست.\n"
                f"پرتاب بعدی را بفرست."
            )

            return

        # =================================================
        # کاربر تمام کرد
        # =================================================

        await run_robot_turn(
            game["id"],
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

        # پرتاب‌ها تمام شده
        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های سازنده تمام شده."
            )
            return

        # باید نوبت سازنده باشد
        if game["creator_round"] > game["opponent_round"]:

            await msg.reply_text(
                "⏳ هنوز نوبت حریف است."
            )
            return

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game["id"],)).fetchone()

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
                log.exception("creator throw error")
                return

        await msg.reply_text(
            f"👤 {display_name(game['creator'])}: {value}\n\n"
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

        # باید سازنده یک پرتاب جلوتر باشد
        if game["creator_round"] <= game["opponent_round"]:

            await msg.reply_text(
                "⏳ هنوز نوبت شما نیست."
            )
            return

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game["id"],)).fetchone()

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
                log.exception("opponent throw error")
                return

        await msg.reply_text(
            f"👤 {display_name(game['opponent'])}: {value}"
        )

    else:
        return

    # =====================================================
    # CHECK FINISH FRIEND
    # =====================================================

    current = get_game(game["id"])

    if not current:
        return

    if (
        current["creator_round"] >= current["rounds"]
        and
        current["opponent_round"] >= current["rounds"]
    ):

        await finish_game(
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
        f"💰 موجودی {q.from_user.full_name}:\n\n"
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
        f"💰 موجودی {user.full_name}:\n\n"
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
        "روی پیام گیرنده Reply کن و فقط بنویس:\n\n"
        "انتقال 1\n\n"
        "مثال:\n"
        "انتقال 0.5"
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
            "❌ برای انتقال باید روی پیام گیرنده Reply کنی."
        )
        return

    user = update.effective_user

    if not user:
        return

    text = digits(msg.text.strip())

    match = re.fullmatch(
        r"^انتقال\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:
        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 0.1"
        )
        return

    try:
        amount = float(match.group(1))
    except Exception:
        await msg.reply_text(
            "❌ مبلغ اشتباه است."
        )
        return

    if amount <= 0:
        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    if amount > 1000000:
        await msg.reply_text(
            "❌ مبلغ بیش از حد مجاز است."
        )
        return

    receiver = msg.reply_to_message.from_user

    if not receiver:
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
            con.execute("BEGIN IMMEDIATE")

            sender = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user.id,)).fetchone()

            receiver_row = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
            """, (receiver.id,)).fetchone()

            if not sender or not receiver_row:
                con.rollback()

                await msg.reply_text(
                    "❌ حساب پیدا نشد."
                )
                return

            sender_balance = float(sender["balance"])

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

            # کسر از فرستنده
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

            # اضافه به گیرنده
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
            log.exception("transfer error")

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
        "برداشت بلاکچینی واقعی در این نسخه فعال نیست.\n"
        "موجودی‌ها فقط TRX مجازی داخل بات هستند."
    )


# =========================================================
# HELP
# =========================================================

async def help_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی:\n"
        "3 تاس 1\n"
        "3 دارت 1\n"
        "3 بسکتبال 1\n"
        "3 بولینگ 1\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام گیرنده Reply کن:\n"
        "انتقال 1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر تمام پرتاب‌هایش را انجام می‌دهد، "
        "بعد ربات تمام پرتاب‌هایش را انجام می‌دهد "
        "و نتیجه نمایش داده می‌شود."
    )


# =========================================================
# ADMIN KEYBOARD
# فقط یک گزینه افزایش موجودی
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
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

async def admin_panel(update, context):

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
        "فقط همین گزینه‌های پنل مدیریت فعال هستند:",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN ADD
# =========================================================

async def admin_add(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    await q.answer()

    context.user_data["admin_action"] = "add"

    await q.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


# =========================================================
# ADMIN REMOVE
# =========================================================

async def admin_remove(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    await q.answer()

    context.user_data["admin_action"] = "remove"

    await q.message.reply_text(
        "➖ کاهش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


# =========================================================
# ADMIN BALANCE
# =========================================================

async def admin_balance(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "💰 موجودی کاربر\n\n"
        "فقط آیدی عددی کاربر را بفرست."
    )


# =========================================================
# ADMIN STATS
# =========================================================

async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    await q.answer()

    with closing(db()) as con:

        users = con.execute("""
        SELECT COUNT(*) AS c
        FROM users
        """).fetchone()["c"]

        games = con.execute("""
        SELECT COUNT(*) AS c
        FROM games
        """).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) AS c
        FROM games
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

        total = con.execute("""
        SELECT COALESCE(SUM(balance),0) AS b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 کل بازی‌ها: {games}\n"
        f"⏳ بازی فعال: {active}\n"
        f"💰 مجموع موجودی: {money(total)} TRX"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update, context):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user or user.id != OWNER_ID:
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
    # ADMIN BALANCE
    # =====================================================

    if action == "balance":

        if not re.fullmatch(
            r"\d+",
            text
        ):
            await msg.reply_text(
                "❌ آیدی باید عددی باشد."
            )
            return

        target = int(text)

        await msg.reply_text(
            "💰 موجودی کاربر:\n\n"
            f"{money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return

    # =====================================================
    # ADD / REMOVE
    # =====================================================

    match = re.fullmatch(
        r"^(\d+)\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "آیدی مبلغ\n\n"
            "مثال:\n"
            "123456789 10"
        )
        return

    target = int(match.group(1))
    amount = float(match.group(2))

    if amount <= 0:
        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    # کاربر باید قبلاً ثبت شده باشد
    if not get_user(target):

        await msg.reply_text(
            "❌ این کاربر هنوز در دیتابیس ثبت نشده است."
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return

    if action == "add":

        ok = change_balance(
            target,
            amount
        )

        operation = "افزایش"

    elif action == "remove":

        ok = change_balance(
            target,
            -amount
        )

        operation = "کاهش"

    else:

        ok = False
        operation = "عملیات"

    if ok:

        await msg.reply_text(
            f"✅ {operation} موجودی انجام شد.\n\n"
            f"🆔 کاربر: {target}\n"
            f"💰 مقدار: {money(amount)} TRX\n"
            f"💳 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await msg.reply_text(
            "❌ عملیات انجام نشد.\n"
            "اگر کاهش موجودی است، احتمالاً موجودی کافی نیست."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


# =========================================================
# CALLBACK ROUTER
# فقط یک CallbackQueryHandler
# =========================================================

async def callback_router(update, context):

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
            "✅ عضویت تأیید شد."
            if ok
            else
            "❌ هنوز عضو کانال نیستی.",
            show_alert=True
        )

        return

    # =====================================================
    # MAIN
    # =====================================================

    if data == "games":
        await show_games(update, context)
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
        await admin_panel(update, context)
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
            reply_markup=main_keyboard(
                q.from_user.id
            )
        )

        return

    # =====================================================
    # GAME
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


# =========================================================
# TEXT ROUTER
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

    # =====================================================
    # ضد پیام‌های خیلی طولانی
    # =====================================================

    if len(text) > 200:
        return

    # =====================================================
    # ADMIN
    #
    # خیلی مهم:
    # فقط وقتی پنل مدیریت واقعاً منتظر ورودی است
    # پیام admin پردازش می‌شود.
    # بنابراین «آیدی مبلغ» به صورت عادی
    # دیگر پیام خطای پنل نمی‌دهد.
    # =====================================================

    if (
        user.id == OWNER_ID
        and
        context.user_data.get("admin_action")
    ):

        await admin_text(
            update,
            context
        )

        return

    # =====================================================
    # موجودی
    # فقط همین یک دستور متنی
    # =====================================================

    if text in (
        "موجودی",
        "بالانس",
        "balance"
    ):

        await balance_text(
            update,
            context
        )

        return

    # =====================================================
    # انتقال
    # فقط فرمت دقیق
    # =====================================================

    if re.fullmatch(
        r"^انتقال\s+\d{1,8}(?:\.\d{1,8})?$",
        text
    ):

        await transfer_handler(
            update,
            context
        )

        return

    # =====================================================
    # بازی
    # =====================================================

    parsed = parse_game(text)

    if parsed:

        await create_game(
            update,
            context
        )

        return

    # =====================================================
    # هر پیام دیگر:
    # هیچ پاسخی نده.
    #
    # این قسمت عمداً خالی است تا:
    # - ضد دستور باشد
    # - پیام‌های اضافی نیاید
    # - «فرمت آیدی مبلغ» بی‌دلیل ظاهر نشود
    # =====================================================

    return


# =========================================================
# ERROR
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
    # START
    # =====================================================

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # =====================================================
    # CALLBACK
    # فقط یک CallbackQueryHandler
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            callback_router
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
            filters.TEXT & ~filters.COMMAND,
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
        "BET_BT BOT STARTED"
    )

    # =====================================================
    # RUN
    # =====================================================

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
