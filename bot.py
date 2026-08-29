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

DB = "bot.sqlite3"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger(__name__)


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

        columns = {
            x["name"]
            for x in con.execute(
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


def get_balance(user_id):

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


def change_balance(user_id, amount):

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

            new_balance = (
                float(row["balance"])
                + float(amount)
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
                "balance error"
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
# GAMES
# =========================================================

GAMES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳",
}


def valid_value(emoji, value):

    value = int(value)

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

    if rounds < 1 or rounds > 100:
        return None

    if amount <= 0 or amount > 1000000:
        return None

    game = match.group(2)

    return {
        "rounds": rounds,
        "game": game,
        "emoji": GAMES[game],
        "amount": round(
            amount,
            8
        )
    }


# =========================================================
# GAME GETTERS
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


def active_user_game(user_id):

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

        return True


async def require_membership(
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
                "🎮 بازی‌ها",
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

    user = update.effective_user

    if not user:
        return

    register(user)

    if not await require_membership(
        update,
        context
    ):
        return

    await update.message.reply_text(

        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "💰 واحد حساب: TRX داخلی و مجازی\n"
        "⛓ بلاکچین واقعی استفاده نمی‌شود.\n\n"
        "🎮 از منوی بازی‌ها استفاده کن.",

        reply_markup=main_keyboard(
            user.id
        )
    )


# =========================================================
# GAMES MENU
# =========================================================

async def games_menu(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(

        "🎮 بازی‌ها\n\n"

        "برای ساخت بازی داخل گپ بنویس:\n\n"

        "🎲 3 تاس 100\n"
        "🎯 3 دارت 100\n"
        "🏀 3 بسکتبال 100\n"
        "🎳 3 بولینگ 100\n\n"

        "🔢 عدد اول = تعداد پرتاب\n"
        "💰 عدد آخر = مبلغ شرط\n\n"

        "👥 بازی با دوستان:\n"
        "بعد از ساخت بازی، دکمه «ورود به بازی» برای همه قابل استفاده است.\n"
        "فقط یک نفر می‌تواند وارد هر بازی شود.\n\n"

        "🤖 بازی با ربات:\n"
        "سازنده می‌تواند ربات را انتخاب کند."
    )


async def games_command(
    update,
    context
):

    if not update.message:
        return

    await update.message.reply_text(

        "🎮 بازی‌ها\n\n"

        "🎲 3 تاس 100\n"
        "🎯 3 دارت 100\n"
        "🏀 3 بسکتبال 100\n"
        "🎳 3 بولینگ 100\n\n"

        "مثال:\n"
        "3 تاس 500"
    )


async def examples(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(

        "🎯 مثال بازی‌ها:\n\n"

        "🎲 3 تاس 100\n"
        "🎯 3 دارت 100\n"
        "🏀 3 بسکتبال 100\n"
        "🎳 3 بولینگ 100"
    )


async def help_menu(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(

        "📖 راهنما\n\n"

        "💰 موجودی:\n"
        "موجودی\n\n"

        "🔄 انتقال در گپ:\n"
        "روی پیام گیرنده Reply کن و بنویس:\n"
        "انتقال 100\n\n"

        "🎮 ساخت بازی:\n"
        "3 تاس 100\n\n"

        "👥 بازی با دوستان:\n"
        "دکمه «ورود به بازی» برای همه باز است.\n"
        "هر بازی فقط یک بازیکن دوم می‌پذیرد.\n\n"

        "🤖 بازی با ربات:\n"
        "سازنده روی «بازی با ربات» می‌زند.\n"
        "بعد از آخرین پرتاب بازیکن، ربات شروع می‌کند."
    )


async def transfer_menu(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(

        "🔄 انتقال\n\n"

        "روی پیام شخصی که می‌خواهی برایش پول بفرستی Reply کن و بنویس:\n\n"

        "انتقال 100"
    )


async def withdraw_menu(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(

        "💸 برداشت\n\n"

        "برداشت بلاکچینی در این نسخه فعال نیست.\n\n"
        "💰 موجودی‌ها داخلی و مجازی هستند."
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

    register(q.from_user)

    await q.message.reply_text(

        f"💰 موجودی {q.from_user.full_name}:\n\n"
        f"{money(get_balance(q.from_user.id))} TRX"
    )


async def balance_command(
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

        f"💰 موجودی {user.full_name}:\n\n"
        f"{money(get_balance(user.id))} TRX"
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

    if not await require_membership(
        update,
        context
    ):
        return

    if active_user_game(
        user.id
    ):

        await msg.reply_text(
            "❌ شما یک بازی فعال دارید."
        )

        return

    amount = parsed["amount"]

    game_id = secrets.token_hex(
        16
    )

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
                return

            if float(row["balance"]) < amount:

                con.rollback()

                await msg.reply_text(

                    "❌ موجودی کافی نیست.\n\n"

                    f"💰 موجودی: "
                    f"{money(row['balance'])} TRX\n"

                    f"💰 شرط: "
                    f"{money(amount)} TRX"
                )

                return

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if con.total_changes != 1:

                con.rollback()
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
                "create game error"
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
            ),

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

        f"{parsed['emoji']} بازی با دوستان ساخته شد!\n\n"

        f"👤 سازنده: "
        f"{display_name(user.id)}\n"

        f"🎮 بازی: "
        f"{parsed['game']}\n"

        f"🔢 تعداد پرتاب: "
        f"{parsed['rounds']}\n"

        f"💰 شرط: "
        f"{money(amount)} TRX\n\n"

        "👥 هر بازیکنی می‌تواند روی «ورود به بازی» بزند.\n"
        "⚠️ فقط یک نفر می‌تواند وارد این بازی شود.\n\n"

        "🤖 یا سازنده می‌تواند با ربات بازی کند.",

        reply_markup=keyboard
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(
    update,
    context
):

    q = update.callback_query

    user = q.from_user

    if not user:
        await q.answer()
        return

    register(user)

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

    if game["status"] != "waiting":

        await q.answer(
            "❌ این بازی قبلاً توسط یک نفر گرفته شده.",
            show_alert=True
        )

        return

    if user.id == game["creator"]:

        await q.answer(
            "❌ سازنده نمی‌تواند وارد بازی خودش شود.",
            show_alert=True
        )

        return

    if active_user_game(
        user.id
    ):

        await q.answer(
            "❌ شما یک بازی فعال دارید.",
            show_alert=True
        )

        return

    if not await member_ok(
        context.bot,
        user.id
    ):

        await q.answer(
            "❌ ابتدا عضو کانال شوید.",
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

            # =================================================
            # این قسمت تضمین می‌کند فقط یک نفر وارد شود
            # =================================================

            if (
                not current
                or
                current["status"] != "waiting"
            ):

                con.rollback()

                await q.answer(
                    "❌ یک نفر دیگر قبلاً وارد بازی شده.",
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

            if (
                not row
                or
                float(row["balance"]) < amount
            ):

                con.rollback()

                await q.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if con.total_changes != 1:

                con.rollback()

                await q.answer(
                    "❌ ورود انجام نشد.",
                    show_alert=True
                )

                return

            con.execute("""
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

            if con.total_changes != 1:

                con.rollback()

                # در صورت رقابت همزمان پول برگشت داده می‌شود
                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount,
                    user.id
                ))

                con.commit()

                await q.answer(
                    "❌ این بازی توسط شخص دیگری گرفته شد.",
                    show_alert=True
                )

                return

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "join error"
            )

            await q.answer(
                "❌ خطا در ورود به بازی.",
                show_alert=True
            )

            return

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(

        "🎮 بازی شروع شد!\n\n"

        f"👤 بازیکن اول: "
        f"{display_name(game['creator'])}\n"

        f"👤 بازیکن دوم: "
        f"{display_name(user.id)}\n\n"

        f"{game['emoji']} بازی: "
        f"{game['game']}\n"

        f"🔢 تعداد پرتاب: "
        f"{game['rounds']}\n"

        f"💰 شرط هر نفر: "
        f"{money(amount)} TRX\n\n"

        f"👉 نوبت "
        f"{display_name(game['creator'])}"
        " است."
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

    if not user:
        await q.answer()
        return

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
            "❌ فقط سازنده می‌تواند با ربات بازی کند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده.",
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

            if (
                not current
                or
                current["status"] != "waiting"
            ):

                con.rollback()

                await q.answer(
                    "❌ این بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            con.execute("""
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

            if con.total_changes != 1:

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
                "robot start error"
            )

            await q.answer(
                "❌ خطا.",
                show_alert=True
            )

            return

    await q.answer(
        "🤖 بازی با ربات شروع شد."
    )

    await q.message.reply_text(

        "🤖 بازی با ربات شروع شد!\n\n"

        f"👤 بازیکن: "
        f"{display_name(user.id)}\n"

        "🤖 حریف: ربات\n"

        f"{game['emoji']} بازی: "
        f"{game['game']}\n"

        f"🔢 تعداد پرتاب: "
        f"{game['rounds']}\n"

        f"💰 شرط: "
        f"{money(game['amount'])} TRX\n\n"

        f"👤 حالا تمام {game['rounds']} پرتاب خودت را انجام بده.\n"

        "⚡ بعد از آخرین پرتاب، ربات سریع شروع می‌کند "
        "و نتیجه اعلام می‌شود."
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
            "❌ بازی شروع شده.",
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

            if (
                not current
                or
                current["status"] != "waiting"
            ):

                con.rollback()
                return

            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (
                game_id,
            ))

            if con.total_changes != 1:

                con.rollback()
                return

            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                float(current["amount"]),
                current["creator"]
            ))

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "cancel error"
            )

            return

    await q.answer(
        "✅ بازی لغو شد."
    )

    await q.message.reply_text(

        "❌ بازی لغو شد.\n\n"

        f"💰 "
        f"{money(game['amount'])} TRX "
        "به سازنده برگشت."
    )


# =========================================================
# ROBOT THROW
# =========================================================

async def robot_throw(
    context,
    game_id
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

    if (
        game["opponent_round"]
        >=
        game["rounds"]
    ):
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

    if not valid_value(
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

            if (
                current["opponent_round"]
                >=
                current["rounds"]
            ):

                con.rollback()
                return None

            con.execute("""
            UPDATE games
            SET
                opponent_round=opponent_round+1,
                opponent_score=opponent_score+?
            WHERE id=?
            AND status='playing'
            AND opponent_round<rounds
            """, (
                value,
                game_id
            ))

            if con.total_changes != 1:

                con.rollback()
                return None

            con.commit()

            return value

        except Exception:

            con.rollback()

            log.exception(
                "robot database error"
            )

            return None


# =========================================================
# FINISH GAME
# =========================================================

async def finish_game(
    context,
    game_id
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
                return False

            if game["status"] != "playing":

                con.rollback()
                return False

            if (
                game["creator_round"]
                <
                game["rounds"]
                or
                game["opponent_round"]
                <
                game["rounds"]
            ):

                con.rollback()
                return False

            con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
            AND status='playing'
            """, (
                game_id,
            ))

            if con.total_changes != 1:

                con.rollback()
                return False

            creator_score = int(
                game["creator_score"]
            )

            opponent_score = int(
                game["opponent_score"]
            )

            amount = float(
                game["amount"]
            )

            pot = amount * 2

            if creator_score > opponent_score:

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    pot,
                    game["creator"]
                ))

                winner_text = (

                    f"🏆 برنده: "
                    f"{display_name(game['creator'])}\n\n"

                    f"💰 جایزه: "
                    f"{money(pot)} TRX"
                )

            elif opponent_score > creator_score:

                if game["mode"] == "friend":

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        pot,
                        game["opponent"]
                    ))

                    winner_text = (

                        f"🏆 برنده: "
                        f"{display_name(game['opponent'])}\n\n"

                        f"💰 جایزه: "
                        f"{money(pot)} TRX"
                    )

                else:

                    winner_text = (

                        "🏆 برنده: 🤖 ربات\n\n"

                        "💰 ربات موجودی واقعی ندارد."
                    )

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

                    winner_text = (

                        "🤝 بازی مساوی شد.\n\n"

                        f"💰 {money(amount)} TRX "
                        "به هر نفر برگشت."
                    )

                else:

                    winner_text = (

                        "🤝 بازی مساوی شد.\n\n"

                        f"💰 {money(amount)} TRX "
                        "به بازیکن برگشت."
                    )

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "finish error"
            )

            return False

    creator_name = display_name(
        game["creator"]
    )

    opponent_name = (
        "🤖 ربات"
        if game["mode"] == "robot"
        else display_name(
            game["opponent"]
        )
    )

    text = (

        f"{game['emoji']} نتیجه بازی\n\n"

        f"👤 بازیکن اول: "
        f"{creator_name}\n"

        f"🤖 حریف: "
        f"{opponent_name}\n\n"

        f"🎮 بازی: "
        f"{game['game']}\n"

        f"🔢 تعداد پرتاب: "
        f"{game['rounds']}\n\n"

        "📊 امتیاز نهایی:\n\n"

        f"👤 {creator_name}: "
        f"{creator_score}\n"

        f"🤖 {opponent_name}: "
        f"{opponent_score}\n\n"

        f"{winner_text}"
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )

    return True


# =========================================================
# GAME DICE HANDLER
# =========================================================

async def game_dice_handler(
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

    if not valid_value(
        emoji,
        value
    ):
        return

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

        if (
            game["creator_round"]
            >=
            game["rounds"]
        ):
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
                    >=
                    current["rounds"]
                ):

                    con.rollback()
                    return

                con.execute("""
                UPDATE games
                SET
                    creator_round=creator_round+1,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                AND creator_round<rounds
                """, (
                    value,
                    game["id"]
                ))

                if con.total_changes != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "user throw error"
                )

                return

        current = get_game(
            game["id"]
        )

        if not current:
            return

        if (
            current["creator_round"]
            <
            current["rounds"]
        ):
            return

        # =================================================
        # ربات سریع شروع می‌کند
        # =================================================

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
                context,
                game["id"]
            )

            if robot_value is None:
                return

        current = get_game(
            game["id"]
        )

        if not current:
            return

        if (
            current["creator_round"]
            >=
            current["rounds"]
            and
            current["opponent_round"]
            >=
            current["rounds"]
        ):

            await finish_game(
                context,
                game["id"]
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
                    >=
                    current["rounds"]
                ):

                    con.rollback()
                    return

                if (
                    current["creator_round"]
                    !=
                    current["opponent_round"]
                ):

                    con.rollback()
                    return

                con.execute("""
                UPDATE games
                SET
                    creator_round=creator_round+1,
                    creator_score=creator_score+?
                WHERE id=?
                AND status='playing'
                AND creator_round<rounds
                AND creator_round=opponent_round
                """, (
                    value,
                    game["id"]
                ))

                if con.total_changes != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "creator throw error"
                )

                return

    # =====================================================
    # OPPONENT
    # =====================================================

    elif user.id == game["opponent"]:

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
                    >=
                    current["rounds"]
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

                con.execute("""
                UPDATE games
                SET
                    opponent_round=opponent_round+1,
                    opponent_score=opponent_score+?
                WHERE id=?
                AND status='playing'
                AND opponent_round<rounds
                AND creator_round>opponent_round
                """, (
                    value,
                    game["id"]
                ))

                if con.total_changes != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "opponent throw error"
                )

                return

    else:
        return

    # =====================================================
    # FINISH
    # =====================================================

    current = get_game(
        game["id"]
    )

    if not current:
        return

    if (
        current["creator_round"]
        >=
        current["rounds"]
        and
        current["opponent_round"]
        >=
        current["rounds"]
    ):

        await finish_game(
            context,
            game["id"]
        )


# =========================================================
# TRANSFER BY REPLY
# =========================================================

async def transfer_handler(
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

        await msg.reply_text(
            "❌ انتقال با Reply فقط در گپ انجام می‌شود."
        )

        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ روی پیام گیرنده Reply کن.\n\n"
            "مثال:\n"
            "انتقال 100"
        )

        return

    user = update.effective_user

    if not user:
        return

    text = digits(
        msg.text.strip()
    )

    match = re.fullmatch(
        r"^انتقال\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 100"
        )

        return

    try:

        amount = float(
            match.group(1)
        )

    except Exception:
        return

    if amount <= 0 or amount > 1000000:

        await msg.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    receiver = (
        msg.reply_to_message.from_user
    )

    if not receiver:
        return

    if receiver.id == user.id:

        await msg.reply_text(
            "❌ انتقال به خودت ممکن نیست."
        )

        return

    if receiver.is_bot:

        await msg.reply_text(
            "❌ انتقال به ربات ممکن نیست."
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

            if (
                not sender
                or
                float(sender["balance"])
                <
                amount
            ):

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            AND balance>=?
            """, (
                amount,
                user.id,
                amount
            ))

            if con.total_changes != 1:

                con.rollback()
                return

            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id
            ))

            if con.total_changes != 1:

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
                "❌ انتقال انجام نشد."
            )

            return

    await msg.reply_text(

        "✅ انتقال با موفقیت انجام شد.\n\n"

        f"👤 گیرنده: "
        f"{receiver.full_name}\n"

        f"💰 مقدار: "
        f"{money(amount)} TRX\n"

        f"💳 موجودی شما: "
        f"{money(get_balance(user.id))} TRX"
    )


# =========================================================
# RESET ACTIVE GAMES
# فقط OWNER
# فقط یک دستور: ریست
# هم گپ و هم PV
# =========================================================

async def reset_games_command(
    update,
    context
):

    msg = update.message

    user = update.effective_user

    if not user:
        return

    # فقط مالک
    if user.id != OWNER_ID:

        if msg:
            await msg.reply_text(
                "❌ فقط مالک ربات اجازه ریست دارد."
            )

        elif update.callback_query:
            await update.callback_query.answer(
                "❌ فقط مالک ربات اجازه ریست دارد.",
                show_alert=True
            )

        return

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            games = con.execute("""
            SELECT *
            FROM games
            WHERE status IN ('waiting','playing')
            """).fetchall()

            if not games:

                con.commit()

                if msg:

                    await msg.reply_text(
                        "✅ هیچ بازی فعالی وجود ندارد."
                    )

                elif update.callback_query:

                    await update.callback_query.answer(
                        "✅ هیچ بازی فعالی وجود ندارد.",
                        show_alert=True
                    )

                return

            returned = 0.0
            count = 0

            for game in games:

                amount = float(
                    game["amount"]
                )

                # شرط سازنده
                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount,
                    game["creator"]
                ))

                returned += amount

                # شرط بازیکن دوم
                if (
                    game["mode"] == "friend"
                    and
                    game["opponent"]
                    and
                    int(game["opponent"]) != 0
                ):

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        game["opponent"]
                    ))

                    returned += amount

                count += 1

            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE status IN ('waiting','playing')
            """)

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "RESET GAMES ERROR"
            )

            if msg:

                await msg.reply_text(
                    "❌ ریست انجام نشد."
                )

            elif update.callback_query:

                await update.callback_query.answer(
                    "❌ ریست انجام نشد.",
                    show_alert=True
                )

            return

    result_text = (

        "♻️ ریست بازی‌ها انجام شد.\n\n"

        f"🎮 بازی‌های ریست‌شده: "
        f"{count}\n"

        f"💰 مبلغ برگشتی: "
        f"{money(returned)} TRX\n\n"

        "✅ تمام بازی‌های فعال بسته شدند.\n"
        "✅ شرط‌های بلوکه‌شده برگشت داده شدند."
    )

    if msg:

        await msg.reply_text(
            result_text
        )

    elif update.callback_query:

        await update.callback_query.answer(
            "✅ ریست انجام شد.",
            show_alert=True
        )

        await update.callback_query.message.reply_text(
            result_text
        )


# =========================================================
# ADMIN KEYBOARD
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
                "♻️ ریست بازی‌ها",
                callback_data="admin_reset"
            )
        ]
    ])


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(
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

    if q.message.chat.type != ChatType.PRIVATE:

        await q.answer(
            "❌ پنل مدیریت فقط در PV مالک است.",
            show_alert=True
        )

        return

    await q.answer()

    await q.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard()
    )


async def admin_add(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    if q.message.chat.type != ChatType.PRIVATE:
        return

    await q.answer()

    context.user_data[
        "admin_action"
    ] = "add"

    await q.message.reply_text(

        "➕ افزایش موجودی\n\n"

        "آیدی مبلغ\n\n"

        "مثال:\n"
        "123456789 100"
    )


async def admin_remove(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    if q.message.chat.type != ChatType.PRIVATE:
        return

    await q.answer()

    context.user_data[
        "admin_action"
    ] = "remove"

    await q.message.reply_text(
        "➖ کاهش موجودی\n\n"
        "آیدی مبلغ"
    )


async def admin_balance(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    if q.message.chat.type != ChatType.PRIVATE:
        return

    await q.answer()

    context.user_data[
        "admin_action"
    ] = "balance"

    await q.message.reply_text(
        "💰 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(
    update,
    context
):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    if q.message.chat.type != ChatType.PRIVATE:
        return

    await q.answer()

    with closing(db()) as con:

        users = con.execute("""
        SELECT COUNT(*) c
        FROM users
        """).fetchone()["c"]

        games = con.execute("""
        SELECT COUNT(*) c
        FROM games
        """).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) c
        FROM games
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

        total = con.execute("""
        SELECT COALESCE(
            SUM(balance),
            0
        ) b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(

        "📊 آمار\n\n"

        f"👤 کاربران: {users}\n"

        f"🎮 کل بازی‌ها: {games}\n"

        f"⏳ بازی فعال: {active}\n"

        f"💰 مجموع موجودی: "
        f"{money(total)} TRX"
    )


async def admin_reset(
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

    if q.message.chat.type != ChatType.PRIVATE:

        await q.answer(
            "❌ پنل مدیریت فقط در PV مالک است.",
            show_alert=True
        )

        return

    # اینجا تابع جدید درست کار می‌کند
    await reset_games_command(
        update,
        context
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
    # BALANCE
    # =====================================================

    if action == "balance":

        if not re.fullmatch(
            r"\d+",
            text
        ):

            await msg.reply_text(
                "❌ آیدی عددی اشتباه است."
            )

            return

        target = int(text)

        await msg.reply_text(

            "💰 موجودی کاربر:\n\n"

            f"{money(get_balance(target))} TRX"
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

    if not get_user(target):

        await msg.reply_text(
            "❌ کاربر هنوز در دیتابیس ثبت نشده."
        )

        return

    if action == "add":

        ok = change_balance(
            target,
            amount
        )

    else:

        ok = change_balance(
            target,
            -amount
        )

    if ok:

        await msg.reply_text(

            "✅ انجام شد.\n\n"

            f"💰 موجودی جدید:\n"

            f"{money(get_balance(target))} TRX"
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

async def callback_router(
    update,
    context
):

    q = update.callback_query

    if not q:
        return

    data = q.data or ""

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

    if data == "games":

        await games_menu(
            update,
            context
        )

        return

    if data == "examples":

        await examples(
            update,
            context
        )

        return

    if data == "balance":

        await balance_button(
            update,
            context
        )

        return

    if data == "transfer":

        await transfer_menu(
            update,
            context
        )

        return

    if data == "withdraw":

        await withdraw_menu(
            update,
            context
        )

        return

    if data == "help":

        await help_menu(
            update,
            context
        )

        return

    if data == "admin":

        await admin_panel(
            update,
            context
        )

        return

    if data == "admin_add":

        await admin_add(
            update,
            context
        )

        return

    if data == "admin_remove":

        await admin_remove(
            update,
            context
        )

        return

    if data == "admin_balance":

        await admin_balance(
            update,
            context
        )

        return

    if data == "admin_stats":

        await admin_stats(
            update,
            context
        )

        return

    if data == "admin_reset":

        await admin_reset(
            update,
            context
        )

        return

    if data.startswith("join:"):

        await join_game(
            update,
            context
        )

        return

    if data.startswith("robot:"):

        await robot_game(
            update,
            context
        )

        return

    if data.startswith("cancel:"):

        await cancel_game(
            update,
            context
        )

        return


# =========================================================
# TEXT ROUTER
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

    if len(text) > 200:
        return

    # =====================================================
    # ریست
    #
    # فقط یک دستور:
    # ریست
    #
    # هم گپ و هم PV
    # فقط مالک
    # =====================================================

    if text == "ریست":

        if user.id != OWNER_ID:

            await msg.reply_text(
                "❌ فقط مالک ربات اجازه ریست دارد."
            )

            return

        await reset_games_command(
            update,
            context
        )

        return

    # =====================================================
    # ADMIN
    # فقط PV مالک
    # =====================================================

    if (
        user.id == OWNER_ID
        and
        msg.chat.type == ChatType.PRIVATE
        and
        context.user_data.get(
            "admin_action"
        )
    ):

        await admin_text(
            update,
            context
        )

        return

    # =====================================================
    # BALANCE
    # =====================================================

    if text in (
        "موجودی",
        "بالانس",
        "balance"
    ):

        await balance_command(
            update,
            context
        )

        return

    # =====================================================
    # TRANSFER
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
    # GAMES MENU
    # فقط یک عبارت اصلی: بازی
    # =====================================================

    if text in (
        "بازی",
        "گیم",
        "games"
    ):

        if msg.chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await games_command(
                update,
                context
            )

        return

    # =====================================================
    # GAME CREATE
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
    # CALLBACKS
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
            game_dice_handler
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

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
