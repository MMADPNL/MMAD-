import os
import re
import sqlite3
import secrets
import logging
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_ID = 8552447077

CHANNEL = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DB_FILE = "bot.sqlite3"

MAX_ROUNDS = 100
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
        """, (
            user_id,
        )).fetchone()


def balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0.0

    return max(
        0.0,
        float(row["balance"])
    )


def money(value):

    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# =========================================================
# BALANCE CHANGE
# =========================================================

def change_balance(user_id, amount):

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

            new_balance = (
                old_balance + amount
            )

            if new_balance < 0:
                con.rollback()
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                round(new_balance, 8),
                user_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()

            log.exception(
                "balance change error"
            )

            return False


# =========================================================
# DIGITS
# =========================================================

def digits(text):

    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


# =========================================================
# DISPLAY NAME
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


def valid_game_value(
    emoji,
    value
):

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

    text = digits(
        text.strip()
    )

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

        rounds = int(
            match.group(1)
        )

        amount = float(
            match.group(3)
        )

    except Exception:

        return None

    game = match.group(2)

    if rounds < 1:
        return None

    if rounds > MAX_ROUNDS:
        return None

    if amount <= 0:
        return None

    if amount > MAX_AMOUNT:
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


def user_active_game(user_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE status IN ('waiting','playing')
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


def chat_active_game(chat_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND status IN ('waiting','playing')
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            chat_id,
        )).fetchone()


# =========================================================
# MEMBERSHIP
# =========================================================

async def member_ok(
    bot,
    user_id
):

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

        return True


async def membership(
    update,
    context
):

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
                "👑 پنل مالک",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(rows)


# =========================================================
# START
# =========================================================

async def start(
    update,
    context
):

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
        "از دکمه‌های زیر استفاده کن.",
        reply_markup=main_keyboard(
            user.id
        )
    )


# =========================================================
# GAMES MENU
# =========================================================

async def games(
    update,
    context
):

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
        "🎮 بازی\n\n"
        "داخل گپ بنویس:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1",
        reply_markup=keyboard
    )


# =========================================================
# EXAMPLES
# =========================================================

async def examples(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "حداکثر دور: 100\n"
        "واحد شرط: TRX"
    )


# =========================================================
# FRIENDS
# =========================================================

async def friends(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n\n"
        "4 تاس 0.1\n\n"
        "بازیکن دوم روی «ورود به بازی» می‌زند.\n\n"
        "بعد هر دو بازیکن به نوبت بازی می‌کنند."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "ترتیب بازی:\n\n"
        "1️⃣ کاربر تمام پرتاب‌های خودش\n"
        "2️⃣ بعد ربات تمام پرتاب‌های خودش\n"
        "3️⃣ اعلام نتیجه\n\n"
        "برای همه بازی‌ها یکسان است:\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ"
    )


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(
    update,
    context
):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    parsed = parse_game(
        msg.text
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

    # =====================================================
    # فقط یک بازی فعال در هر گپ
    # =====================================================

    active = chat_active_game(
        msg.chat.id
    )

    if active:

        await msg.reply_text(
            "❌ در این گپ یک بازی فعال وجود دارد.\n\n"
            f"🎮 بازی: {active['game']}\n"
            f"👤 سازنده: "
            f"{display_name(active['creator'])}\n\n"
            "⏳ ابتدا همان بازی را تمام یا لغو کنید."
        )

        return

    # =====================================================
    # هر کاربر فقط یک بازی
    # =====================================================

    old_game = user_active_game(
        user.id
    )

    if old_game:

        await msg.reply_text(
            "❌ شما یک بازی فعال دارید."
        )

        return

    amount = parsed["amount"]

    game_id = secrets.token_hex(16)

    # =====================================================
    # کسر شرط اتمیک
    # =====================================================

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

            current_balance = float(
                row["balance"]
            )

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

            result = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if result.rowcount != 1:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            # =================================================
            # ثبت بازی
            # =================================================

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
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]
    ])

    await msg.reply_text(
        f"{parsed['emoji']} بازی ساخته شد.\n\n"
        f"👤 سازنده: "
        f"{display_name(user.id)}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "منتظر بازیکن دوم...",
        reply_markup=keyboard
    )


# =========================================================
# JOIN GAME
# =========================================================

async def join_game(
    update,
    context
):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    user = q.from_user

    register(user)

    game = get_game(
        game_id
    )

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی دیگر قابل ورود نیست.",
            show_alert=True
        )

        return

    if user.id == game["creator"]:

        await q.answer(
            "❌ نمی‌توانی وارد بازی خودت شوی.",
            show_alert=True
        )

        return

    old = user_active_game(
        user.id
    )

    if old:

        await q.answer(
            "❌ شما یک بازی فعال دارید.",
            show_alert=True
        )

        return

    amount = float(
        game["amount"]
    )

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

                await q.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if current["status"] != "waiting":

                con.rollback()

                await q.answer(
                    "❌ یک نفر دیگر وارد بازی شده.",
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

            current_balance = float(
                row["balance"]
            )

            if current_balance < amount:

                con.rollback()

                await q.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            result = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if result.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            result = con.execute("""
            UPDATE games
            SET
                opponent=?,
                mode='friend',
                status='playing'
            WHERE id=?
            AND status='waiting'
            """, (
                user.id,
                game_id
            ))

            if result.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ ورود انجام نشد.",
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
        f"👤 سازنده: "
        f"{display_name(game['creator'])}\n"
        f"👤 حریف: "
        f"{display_name(user.id)}\n"
        f"{game['emoji']} بازی: "
        f"{game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        f"👤 {display_name(game['creator'])} "
        f"اول بازی کند."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(
    update,
    context
):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    user = q.from_user

    game = get_game(
        game_id
    )

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if user.id != game["creator"]:

        await q.answer(
            "❌ فقط سازنده می‌تواند بازی کند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده است.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            result = con.execute("""
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

            if result.rowcount != 1:

                con.rollback()

                await q.answer(
                    "❌ بازی دیگر قابل شروع نیست.",
                    show_alert=True
                )

                return

            con.commit()

        except Exception:

            con.rollback()

            await q.answer(
                "❌ خطا در شروع بازی.",
                show_alert=True
            )

            return

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"👤 بازیکن: "
        f"{display_name(user.id)}\n"
        f"🤖 حریف: ربات\n"
        f"{game['emoji']} بازی: "
        f"{game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: "
        f"{money(game['amount'])} TRX\n\n"
        f"👤 ابتدا هر {game['rounds']} "
        f"پرتاب خودت را انجام بده.\n"
        "بعد از آن ربات شروع می‌کند."
    )


# =========================================================
# CANCEL GAME
# =========================================================

async def cancel_game(
    update,
    context
):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    game = get_game(
        game_id
    )

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
            "❌ بازی شروع شده است.",
            show_alert=True
        )

        return

    amount = float(
        game["amount"]
    )

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            result = con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            if result.rowcount != 1:

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
                game["creator"]
            ))

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "cancel error"
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
        f"💰 {money(amount)} TRX "
        "به موجودی سازنده برگشت."
    )


# =========================================================
# ROBOT THROW
# =========================================================

async def robot_throw(
    game_id,
    context
):

    game = get_game(
        game_id
    )

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

        value = int(
            result.dice.value
        )

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

            result = con.execute("""
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

            if result.rowcount != 1:

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

async def finish_game(
    game_id,
    context
):

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

            if (
                game["creator_round"] < game["rounds"]
                or
                game["opponent_round"] < game["rounds"]
            ):

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

            result = con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
            AND status='playing'
            """, (
                game_id,
            ))

            if result.rowcount != 1:

                con.rollback()
                return

            # =================================================
            # سازنده برنده
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
                    f"🏆 برنده: "
                    f"{display_name(game['creator'])}\n"
                    f"💰 برد: "
                    f"{money(amount)} TRX"
                )

            # =================================================
            # حریف برنده
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
                        f"🏆 برنده: "
                        f"{display_name(game['opponent'])}\n"
                        f"💰 برد: "
                        f"{money(amount)} TRX"
                    )

                else:

                    # ربات پول دریافت نمی‌کند.
                    # در برد ربات، مبلغ کاربر از بین می‌رود.
                    result_text = (
                        "🤖 ربات برنده شد.\n"
                        f"💰 مبلغ بازی: "
                        f"{money(amount)} TRX"
                    )

            # =================================================
            # مساوی
            # =================================================

            else:

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

                else:

                    # بازی ربات مساوی:
                    # شرط کاربر برگشت داده می‌شود.
                    pass

                result_text = (
                    "🤝 بازی مساوی شد.\n"
                    f"💰 {money(amount)} TRX "
                    "برگشت داده شد."
                )

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "finish_game error"
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
        f"👤 حریف: {opponent_name}\n\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "📊 امتیاز:\n"
        f"👤 {creator_name}: "
        f"{creator_score}\n"
        f"👤 {opponent_name}: "
        f"{opponent_score}\n\n"
        f"{result_text}"
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# =========================================================
# DICE / ALL GAME DICE
# =========================================================

async def dice_handler(
    update,
    context
):

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

    value = int(
        dice.value
    )

    if not valid_game_value(
        emoji,
        value
    ):
        return

    # =====================================================
    # پیدا کردن بازی
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
    # ROBOT MODE
    # =====================================================

    if game["mode"] == "robot":

        if user.id != game["creator"]:
            return

        # ضد پرتاب اضافه
        if (
            game["creator_round"]
            >= game["rounds"]
        ):

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده؛ "
                "نوبت ربات است."
            )

            return

        # =================================================
        # ثبت پرتاب کاربر
        # =================================================

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

                if (
                    current["creator_round"]
                    >= current["rounds"]
                ):

                    con.rollback()

                    await msg.reply_text(
                        "⏳ پرتاب‌های شما تمام شده."
                    )

                    return

                result = con.execute("""
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

                if result.rowcount != 1:

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

        await msg.reply_text(
            f"👤 {display_name(user.id)}: "
            f"{value}\n\n"
            f"📊 پرتاب: "
            f"{current['creator_round']}/"
            f"{current['rounds']}"
        )

        # =================================================
        # هنوز نوبت کاربر
        # =================================================

        if (
            current["creator_round"]
            <
            current["rounds"]
        ):

            await msg.reply_text(
                "⏳ هنوز نوبت شماست."
            )

            return

        # =================================================
        # کاربر تمام کرد
        # ربات شروع می‌کند
        # =================================================

        await msg.reply_text(
            "🤖 نوبت ربات شد..."
        )

        while True:

            current = get_game(
                game["id"]
            )

            if not current:
                return

            if current["status"] != "playing":
                return

            if (
                current["opponent_round"]
                >=
                current["rounds"]
            ):
                break

            robot_value = await robot_throw(
                game["id"],
                context
            )

            if robot_value is None:
                return

            await msg.reply_text(
                f"🤖 ربات: "
                f"{robot_value}"
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

            await finish_game(
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

        if (
            game["creator_round"]
            >= game["rounds"]
        ):

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        # اگر سازنده جلوتر باشد،
        # نوبت حریف است.
        if (
            game["creator_round"]
            >
            game["opponent_round"]
        ):

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

                if (
                    current["creator_round"]
                    >= current["rounds"]
                ):

                    con.rollback()
                    return

                if (
                    current["creator_round"]
                    >
                    current["opponent_round"]
                ):

                    con.rollback()
                    return

                result = con.execute("""
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

                if result.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "creator throw error"
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

        if (
            game["opponent_round"]
            >= game["rounds"]
        ):

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

                if (
                    current["opponent_round"]
                    >= current["rounds"]
                ):

                    con.rollback()
                    return

                if (
                    current["creator_round"]
                    <=
                    current["opponent_round"]
                ):

                    con.rollback()
                    return

                result = con.execute("""
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

                if result.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "opponent throw error"
                )

                return

        await msg.reply_text(
            f"👤 {display_name(game['opponent'])}: "
            f"{value}"
        )

    else:

        return

    # =====================================================
    # CHECK FINISH
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

        await finish_game(
            current["id"],
            context
        )


# =========================================================
# BALANCE
# =========================================================

async def balance_button(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    register(
        q.from_user
    )

    await q.message.reply_text(
        f"💰 موجودی "
        f"{q.from_user.full_name}:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


async def balance_text(
    update,
    context
):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    await msg.reply_text(
        f"💰 موجودی "
        f"{user.full_name}:\n\n"
        f"{money(balance(user.id))} TRX"
    )


# =========================================================
# TRANSFER BUTTON
# =========================================================

async def transfer_button(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام گیرنده Reply کن و بنویس:\n\n"
        "انتقال 1\n\n"
        "مثال:\n"
        "انتقال 0.1"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_handler(
    update,
    context
):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user:
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

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
            "❌ فرمت:\n"
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

    if amount <= 0:

        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if amount > MAX_AMOUNT:

        await msg.reply_text(
            "❌ مبلغ بیش از حد مجاز است."
        )

        return

    receiver = (
        msg.reply_to_message.from_user
    )

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

            receiver_row = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
            """, (
                receiver.id,
            )).fetchone()

            if not sender or not receiver_row:

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
                    f"{money(sender_balance)} TRX"
                )

                return

            result = con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if result.rowcount != 1:

                con.rollback()

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

            result = con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id
            ))

            if result.rowcount != 1:

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
        f"👤 گیرنده: "
        f"{receiver.full_name}\n"
        f"💰 مقدار: "
        f"{money(amount)} TRX\n"
        f"💳 موجودی شما: "
        f"{money(balance(user.id))} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی فعال نیست."
    )


# =========================================================
# HELP
# =========================================================

async def help_button(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 بازی:\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام گیرنده Reply کن:\n"
        "انتقال 0.1\n\n"
        "🤖 بازی ربات:\n"
        "اول تمام پرتاب‌های کاربر، "
        "بعد تمام پرتاب‌های ربات."
    )


# =========================================================
# ADMIN KEYBOARD
# فقط یک دکمه افزایش موجودی
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 افزایش موجودی",
                callback_data="admin_add"
            )
        ],
        [
            InlineKeyboardButton(
                "➖ کاهش موجودی",
                callback_data="admin_remove"
            )
        ],
        [
            InlineKeyboardButton(
                "🔎 موجودی کاربر",
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

async def admin(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:

        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    await q.answer()

    await q.message.reply_text(
        "👑 پنل مالک\n\n"
        "فقط یک روش افزایش موجودی فعال است.",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN ADD
# فقط همین یک مسیر افزایش
# =========================================================

async def admin_add(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "add"

    await q.message.reply_text(
        "💰 افزایش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ\n\n"
        "مثال:\n"
        "123456789 10"
    )


# =========================================================
# ADMIN REMOVE
# =========================================================

async def admin_remove(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "remove"

    await q.message.reply_text(
        "➖ کاهش موجودی\n\n"
        "فرمت:\n"
        "آیدی مبلغ"
    )


# =========================================================
# ADMIN BALANCE
# =========================================================

async def admin_balance(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


# =========================================================
# ADMIN STATS
# =========================================================

async def admin_stats(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
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
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

        total_balance = con.execute("""
        SELECT COALESCE(SUM(balance),0) AS b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games_count}\n"
        f"⏳ فعال: {active}\n"
        f"💰 مجموع موجودی: "
        f"{money(total_balance)} TRX"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(
    update,
    context
):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    if not user:
        return

    if user.id != OWNER_ID:
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
    # BALANCE
    # =====================================================

    if action == "balance":

        if not re.fullmatch(
            r"\d+",
            text
        ):

            await msg.reply_text(
                "❌ آیدی اشتباه است."
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
        r"^(\d+)\s+"
        r"(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "آیدی مبلغ"
        )

        return

    target = int(
        match.group(1)
    )

    amount = float(
        match.group(2)
    )

    if amount <= 0:

        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if amount > MAX_AMOUNT:

        await msg.reply_text(
            "❌ مبلغ بیش از حد مجاز است."
        )

        return

    if not get_user(target):

        await msg.reply_text(
            "❌ کاربر هنوز در دیتابیس نیست."
        )

        return

    # =====================================================
    # فقط یک مسیر برای افزایش
    # =====================================================

    if action == "add":

        ok = change_balance(
            target,
            amount
        )

    elif action == "remove":

        ok = change_balance(
            target,
            -amount
        )

    else:

        ok = False

    if ok:

        await msg.reply_text(
            "✅ عملیات انجام شد.\n\n"
            f"👤 کاربر: {target}\n"
            f"💰 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await msg.reply_text(
            "❌ عملیات انجام نشد."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback(
    update,
    context
):

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

        await games(
            update,
            context
        )

    elif data == "examples":

        await examples(
            update,
            context
        )

    elif data == "friends":

        await friends(
            update,
            context
        )

    elif data == "robot_help":

        await robot_help(
            update,
            context
        )

    elif data == "balance":

        await balance_button(
            update,
            context
        )

    elif data == "transfer":

        await transfer_button(
            update,
            context
        )

    elif data == "withdraw":

        await withdraw(
            update,
            context
        )

    elif data == "help":

        await help_button(
            update,
            context
        )

    elif data == "admin":

        await admin(
            update,
            context
        )

    # =====================================================
    # فقط یک callback برای افزایش
    # =====================================================

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

    elif data == "home":

        await q.answer()

        await q.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=main_keyboard(
                q.from_user.id
            )
        )

    # =====================================================
    # GAME
    # =====================================================

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


# =========================================================
# TEXT ROUTER
# فقط یک MessageHandler برای متن
# =========================================================

async def text_router(
    update,
    context
):

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

    # ضد پیام خیلی طولانی
    if len(text) > 200:
        return

    # =====================================================
    # اول پنل مالک
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
    # فقط دستور موجودی
    # =====================================================

    if text == "موجودی":

        await balance_text(
            update,
            context
        )

        return

    # =====================================================
    # فقط دستور انتقال
    # =====================================================

    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # =====================================================
    # بازی
    # =====================================================

    parsed = parse_game(
        text
    )

    if parsed:

        await create_game(
            update,
            context
        )

        return

    # =====================================================
    # هر متن دیگر نادیده گرفته می‌شود
    # =====================================================


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context
):

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
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # =====================================================
    # DICE / DART / BASKETBALL / BOWLING
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # =====================================================
    # تنها هندلر متن
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
        "BET_BT started"
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
