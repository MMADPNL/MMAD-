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
            amount REAL NOT NULL DEFAULT 0,
            creator_round INTEGER NOT NULL DEFAULT 0,
            opponent_round INTEGER NOT NULL DEFAULT 0,
            creator_score INTEGER NOT NULL DEFAULT 0,
            opponent_score INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.commit()


# =========================================================
# HELPERS
# =========================================================

def digits(text):
    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


def money(value):
    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


def register(user):
    if not user:
        return

    with closing(db()) as con:
        con.execute("""
        INSERT INTO users(user_id, name, username)
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


def display_name(user_id):
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


def add_balance(user_id, amount):
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
            log.exception("balance error")
            return False


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
# PARSER
# فقط همین فرمت ساخت بازی معتبر است
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
# GAME QUERIES
# =========================================================

def get_game(game_id):
    with closing(db()) as con:
        return con.execute("""
        SELECT *
        FROM games
        WHERE id=?
        """, (game_id,)).fetchone()


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
# KEYBOARD
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
                "🎯 مثال",
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

    if not await membership(update, context):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=main_keyboard(user.id)
    )


# =========================================================
# GAME MENU
# =========================================================

async def games_menu(update, context):
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
        "ساخت بازی در گپ:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1",
        reply_markup=keyboard
    )


async def examples(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text(
        "🎯 مثال:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "واحد: TRX داخلی بات"
    )


async def friends_help(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "داخل گپ بنویس:\n"
        "4 تاس 0.1\n\n"
        "بعد نفر دوم روی ورود به بازی می‌زند.\n\n"
        "نوبت‌ها یکی‌یکی انجام می‌شود."
    )


async def robot_help(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "هر ۴ بازی همین منطق را دارند:\n\n"
        "1️⃣ اول کاربر تمام پرتاب‌های خودش را می‌اندازد.\n"
        "2️⃣ تا وقتی پرتاب‌های کاربر تمام نشده ربات هیچ پرتابی نمی‌کند.\n"
        "3️⃣ بعد از آخرین پرتاب کاربر، ربات تمام پرتاب‌های خودش را انجام می‌دهد.\n"
        "4️⃣ سپس نتیجه حساب می‌شود."
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
    if active_user_game(user.id):
        await msg.reply_text(
            "❌ شما یک بازی فعال دارید."
        )
        return

    amount = parsed["amount"]

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
                return

            current_balance = float(row["balance"])

            if current_balance < amount:
                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: {money(current_balance)} TRX\n"
                    f"🎯 شرط: {money(amount)} TRX"
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
                return

            game_id = secrets.token_hex(16)

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
                mode,
                status
            )
            VALUES(
                ?, ?, ?, NULL, ?, ?, ?, ?,
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
            log.exception("create game")
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
        f"👤 سازنده: {display_name(user.id)}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "منتظر بازیکن دوم...",
        reply_markup=keyboard
    )


# =========================================================
# JOIN
# =========================================================

async def join_game(update, context):
    q = update.callback_query
    user = q.from_user

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

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

    register(user)

    if active_user_game(user.id):
        await q.answer(
            "❌ شما یک بازی فعال دارید.",
            show_alert=True
        )
        return

    amount = float(game["amount"])

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            current = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not current or current["status"] != "waiting":
                con.rollback()
                await q.answer(
                    "❌ بازی پر شده.",
                    show_alert=True
                )
                return

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user.id,)).fetchone()

            if not row or float(row["balance"]) < amount:
                con.rollback()
                await q.answer(
                    "❌ موجودی کافی نیست.",
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
                    "❌ موجودی کافی نیست.",
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
                    "❌ ورود انجام نشد.",
                    show_alert=True
                )
                return

            con.commit()

        except Exception:
            con.rollback()
            log.exception("join")
            await q.answer(
                "❌ خطا.",
                show_alert=True
            )
            return

    await q.answer("✅ وارد شدی.")

    await q.message.reply_text(
        "🎮 بازی شروع شد!\n\n"
        f"👤 سازنده: {display_name(game['creator'])}\n"
        f"👤 حریف: {display_name(user.id)}\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        f"👤 نوبت {display_name(game['creator'])} است."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(update, context):
    q = update.callback_query
    user = q.from_user

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:
        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )
        return

    if user.id != game["creator"]:
        await q.answer(
            "❌ فقط سازنده.",
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
            con.execute("BEGIN IMMEDIATE")

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
                    "❌ بازی دیگر قابل شروع نیست.",
                    show_alert=True
                )
                return

            con.commit()

        except Exception:
            con.rollback()
            await q.answer(
                "❌ خطا.",
                show_alert=True
            )
            return

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"👤 بازیکن: {display_name(user.id)}\n"
        "🤖 حریف: ربات\n\n"
        f"{game['emoji']} {game['game']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        "⚠️ اول تمام پرتاب‌های خودت را بفرست.\n"
        "بعد از آخرین پرتاب، ربات شروع می‌کند."
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_game(update, context):
    q = update.callback_query
    user = q.from_user

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:
        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )
        return

    if user.id != game["creator"]:
        await q.answer(
            "❌ فقط سازنده.",
            show_alert=True
        )
        return

    if game["status"] != "waiting":
        await q.answer(
            "❌ بازی شروع شده.",
            show_alert=True
        )
        return

    amount = float(game["amount"])

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            cur = con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
            """, (game_id,))

            if cur.rowcount != 1:
                con.rollback()
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
            log.exception("cancel")
            return

    await q.answer("✅ لغو شد.")

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
        result = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=game["emoji"]
        )

        value = int(result.dice.value)

    except Exception:
        log.exception("robot throw")
        return None

    if not valid_value(game["emoji"], value):
        return None

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

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
            log.exception("robot save")
            return None


# =========================================================
# FINISH
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

            cur = con.execute("""
            UPDATE games
            SET status='finished'
            WHERE id=?
            AND status='playing'
            """, (game_id,))

            if cur.rowcount != 1:
                con.rollback()
                return

            creator_score = int(game["creator_score"])
            opponent_score = int(game["opponent_score"])
            amount = float(game["amount"])

            if creator_score > opponent_score:
                winner_text = (
                    f"🏆 برنده: {display_name(game['creator'])}\n"
                    f"💰 جایزه: {money(amount * 2)} TRX"
                )

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount * 2,
                    game["creator"]
                ))

            elif opponent_score > creator_score:

                if game["mode"] == "robot":
                    winner_text = (
                        "🤖 برنده: ربات\n"
                        f"💰 مبلغ شرط: {money(amount)} TRX"
                    )
                else:
                    winner_text = (
                        f"🏆 برنده: {display_name(game['opponent'])}\n"
                        f"💰 جایزه: {money(amount * 2)} TRX"
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount * 2,
                        game["opponent"]
                    ))

            else:
                winner_text = (
                    "🤝 بازی مساوی شد.\n"
                    f"💰 {money(amount)} TRX برگشت داده شد."
                )

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

            con.commit()

        except Exception:
            con.rollback()
            log.exception("finish")
            return

    creator = display_name(game["creator"])

    opponent = (
        "🤖 ربات"
        if game["mode"] == "robot"
        else display_name(game["opponent"])
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"{game['emoji']} نتیجه بازی\n\n"
            f"👤 سازنده: {creator}\n"
            f"👤 حریف: {opponent}\n\n"
            f"🎮 بازی: {game['game']}\n"
            f"🔢 دور: {game['rounds']}\n\n"
            f"📊 امتیاز:\n"
            f"👤 {creator}: {creator_score}\n"
            f"👤 {opponent}: {opponent_score}\n\n"
            f"{winner_text}"
        )
    )


# =========================================================
# DICE / DART / BASKETBALL / BOWLING
# یک هندلر برای هر ۴ بازی
# =========================================================

async def game_throw_handler(update, context):
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

    if not valid_value(emoji, value):
        return

    # =====================================================
    # پیدا کردن فقط بازی مربوط به همین کاربر و همین ایموجی
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
        with closing(db()) as con:
            current = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            AND status='playing'
            """, (game["id"],)).fetchone()

        if not current:
            return

        if current["creator_round"] >= current["rounds"]:
            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده؛ "
                "نوبت ربات است."
            )
            return

        # ثبت فقط یک پرتاب
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

                # ضد دوباره‌ثبت‌شدن
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
                log.exception("user throw")
                return

        current = get_game(game["id"])

        if not current:
            return

        await msg.reply_text(
            f"👤 {display_name(user.id)}: {value}\n"
            f"📊 پرتاب شما: "
            f"{current['creator_round']}/{current['rounds']}"
        )

        # هنوز نوبت کاربر است
        if current["creator_round"] < current["rounds"]:
            await msg.reply_text(
                "⏳ هنوز نوبت شماست.\n"
                "پرتاب بعدی را بفرست."
            )
            return

        # =================================================
        # کاربر تمام کرد -> ربات
        # =================================================

        await msg.reply_text(
            "✅ تمام پرتاب‌های شما انجام شد.\n"
            "🤖 حالا نوبت ربات است..."
        )

        while True:
            current = get_game(game["id"])

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
                return

            await msg.reply_text(
                f"🤖 ربات: {robot_value}"
            )

        current = get_game(game["id"])

        if (
            current
            and current["creator_round"] >= current["rounds"]
            and current["opponent_round"] >= current["rounds"]
        ):
            await finish_game(
                game["id"],
                context
            )

        return

    # =====================================================
    # FRIEND MODE
    # =====================================================

    if game["mode"] != "friend":
        return

    # -----------------------------------------------------
    # سازنده
    # -----------------------------------------------------

    if user.id == game["creator"]:

        if game["creator_round"] >= game["rounds"]:
            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )
            return

        # اگر سازنده یک پرتاب جلو باشد،
        # حریف هنوز باید بازی کند.
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

                if current["creator_round"] >= current["rounds"]:
                    con.rollback()
                    return

                if current["creator_round"] > current["opponent_round"]:
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
                return

        await msg.reply_text(
            f"👤 {display_name(game['creator'])}: {value}\n"
            "⏳ نوبت حریف."
        )

    # -----------------------------------------------------
    # حریف
    # -----------------------------------------------------

    elif user.id == game["opponent"]:

        if game["opponent_round"] >= game["rounds"]:
            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )
            return

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

                if current["opponent_round"] >= current["rounds"]:
                    con.rollback()
                    return

                if current["creator_round"] <= current["opponent_round"]:
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
                return

        await msg.reply_text(
            f"👤 {display_name(game['opponent'])}: {value}"
        )

    else:
        return

    # -----------------------------------------------------
    # پایان
    # -----------------------------------------------------

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
# BALANCE
# =========================================================

async def balance_text(update, context):
    user = update.effective_user

    if not user:
        return

    register(user)

    await update.message.reply_text(
        f"💰 موجودی {user.full_name}:\n\n"
        f"{money(balance(user.id))} TRX"
    )


async def balance_button(update, context):
    q = update.callback_query
    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        f"💰 موجودی:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_help(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام گیرنده Reply کن و فقط بنویس:\n\n"
        "انتقال 1\n\n"
        "مثال:\n"
        "انتقال 0.1"
    )


async def transfer_handler(update, context):
    msg = update.message
    user = update.effective_user

    if not msg or not user:
        return

    if not msg.reply_to_message:
        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )
        return

    text = digits(msg.text.strip())

    match = re.fullmatch(
        r"^انتقال\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:
        await msg.reply_text(
            "❌ فرمت صحیح:\nانتقال 0.1"
        )
        return

    amount = float(match.group(1))

    if amount <= 0 or amount > 1000000:
        await msg.reply_text(
            "❌ مبلغ نامعتبر."
        )
        return

    receiver = msg.reply_to_message.from_user

    if not receiver:
        return

    if receiver.id == user.id:
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

            if not sender:
                con.rollback()
                return

            if float(sender["balance"]) < amount:
                con.rollback()

                await msg.reply_text(
                    f"❌ موجودی کافی نیست.\n"
                    f"💰 موجودی: {money(sender['balance'])} TRX"
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
                return

            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id
            ))

            con.commit()

        except Exception:
            con.rollback()
            log.exception("transfer")
            return

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {receiver.full_name}\n"
        f"💰 مقدار: {money(amount)} TRX\n"
        f"💳 موجودی شما: {money(balance(user.id))} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):
    q = update.callback_query
    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی در این نسخه فعال نیست."
    )


# =========================================================
# HELP
# =========================================================

async def help_menu(update, context):
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
        "روی پیام Reply:\n"
        "انتقال 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر تمام پرتاب‌ها را می‌اندازد، "
        "بعد ربات."
    )


# =========================================================
# ADMIN
# =========================================================

def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
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


async def admin_menu(update, context):
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
        "یک گزینه را انتخاب کن:",
        reply_markup=admin_keyboard()
    )


async def admin_add(update, context):
    q = update.callback_query

    if q.from_user.id != OWNER_ID:
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


async def admin_remove(update, context):
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


async def admin_balance(update, context):
    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(update, context):
    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    with closing(db()) as con:
        users = con.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        games = con.execute(
            "SELECT COUNT(*) c FROM games"
        ).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) c
        FROM games
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

        total = con.execute("""
        SELECT COALESCE(SUM(balance),0) b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games}\n"
        f"⏳ فعال: {active}\n"
        f"💰 مجموع موجودی: {money(total)} TRX"
    )


async def admin_text(update, context):
    user = update.effective_user
    msg = update.message

    if not user or not msg:
        return

    if user.id != OWNER_ID:
        return

    action = context.user_data.get("admin_action")

    if not action:
        return

    text = digits(msg.text.strip())

    if action == "balance":
        if not re.fullmatch(r"\d+", text):
            await msg.reply_text(
                "❌ آیدی اشتباه است."
            )
            return

        target = int(text)

        await msg.reply_text(
            f"💰 موجودی کاربر:\n"
            f"{money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None
        )
        return

    match = re.fullmatch(
        r"^(\d+)\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not match:
        await msg.reply_text(
            "❌ فرمت:\nآیدی مبلغ"
        )
        return

    target = int(match.group(1))
    amount = float(match.group(2))

    if amount <= 0:
        await msg.reply_text(
            "❌ مبلغ نامعتبر."
        )
        return

    if not get_user(target):
        await msg.reply_text(
            "❌ کاربر در دیتابیس نیست."
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
            "✅ انجام شد.\n\n"
            f"💰 موجودی جدید:\n"
            f"{money(balance(target))} TRX"
        )
    else:
        await msg.reply_text(
            "❌ عملیات انجام نشد؛ "
            "موجودی کافی نیست."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


# =========================================================
# CALLBACK
# =========================================================

async def callback(update, context):
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
        await games_menu(update, context)

    elif data == "examples":
        await examples(update, context)

    elif data == "friends":
        await friends_help(update, context)

    elif data == "robot_help":
        await robot_help(update, context)

    elif data == "balance":
        await balance_button(update, context)

    elif data == "transfer":
        await transfer_help(update, context)

    elif data == "withdraw":
        await withdraw(update, context)

    elif data == "help":
        await help_menu(update, context)

    elif data == "admin":
        await admin_menu(update, context)

    elif data == "admin_add":
        await admin_add(update, context)

    elif data == "admin_remove":
        await admin_remove(update, context)

    elif data == "admin_balance":
        await admin_balance(update, context)

    elif data == "admin_stats":
        await admin_stats(update, context)

    elif data == "home":
        await q.answer()

        await q.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=main_keyboard(
                q.from_user.id
            )
        )

    elif data.startswith("join:"):
        await join_game(update, context)

    elif data.startswith("robot:"):
        await robot_game(update, context)

    elif data.startswith("cancel:"):
        await cancel_game(update, context)


# =========================================================
# TEXT ROUTER
# فقط یک MessageHandler برای متن
# =========================================================

async def text_router(update, context):
    msg = update.message

    if not msg or not msg.text:
        return

    user = update.effective_user

    if not user:
        return

    register(user)

    text = digits(msg.text.strip())

    # پیام‌های خیلی طولانی نادیده گرفته می‌شوند
    if len(text) > 200:
        return

    # -----------------------------------------------------
    # اول پنل مدیریت
    # -----------------------------------------------------

    if (
        user.id == OWNER_ID
        and context.user_data.get("admin_action")
    ):
        await admin_text(
            update,
            context
        )
        return

    # -----------------------------------------------------
    # موجودی - فقط یک دستور
    # -----------------------------------------------------

    if text == "موجودی":
        await balance_text(
            update,
            context
        )
        return

    # -----------------------------------------------------
    # انتقال - فقط یک دستور
    # -----------------------------------------------------

    if text.startswith("انتقال "):
        await transfer_handler(
            update,
            context
        )
        return

    # -----------------------------------------------------
    # ساخت بازی - فقط فرمت اصلی
    # -----------------------------------------------------

    parsed = parse_game(text)

    if parsed:
        await create_game(
            update,
            context
        )
        return

    # هیچ دستور متنی دیگری اجرا نمی‌شود


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

    # فقط یک /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # فقط یک callback router
    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # هر چهار بازی از یک هندلر
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_throw_handler
        )
    )

    # فقط یک text router
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router
        )
    )

    app.add_error_handler(
        error_handler
    )

    log.info("BET_BT BOT STARTED")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
