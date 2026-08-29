import os
import sqlite3
import secrets
import logging
import re
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
# USERS
# =========================================================

def register(user):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO users (
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


def get_user_name(user_id):

    if user_id == 0:
        return "ربات"

    with closing(db()) as con:

        row = con.execute("""
        SELECT name, username
        FROM users
        WHERE user_id=?
        """, (
            user_id,
        )).fetchone()

    if not row:
        return str(user_id)

    if row["name"]:
        return row["name"]

    if row["username"]:
        return "@" + row["username"]

    return str(user_id)


def balance(user_id):

    with closing(db()) as con:

        row = con.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
        """, (
            user_id,
        )).fetchone()

    if not row:
        return 0.0

    return float(row["balance"])


def add_balance(user_id, amount):

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
                "balance update error"
            )

            return False


def money(value):

    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


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
    "بولینگ": "🎳"
}

EMOJI_TO_GAME = {
    "🎲": "تاس",
    "🎯": "دارت",
    "🏀": "بسکتبال",
    "🎳": "بولینگ"
}


# =========================================================
# GAME PARSER
# =========================================================

def parse_game(text):

    text = digits(
        text.strip()
    )

    m = re.fullmatch(
        r"(\d+)\s+(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)",
        text
    )

    if not m:
        return None

    rounds = int(
        m.group(1)
    )

    game = m.group(2)

    amount = float(
        m.group(3)
    )

    if rounds <= 0:
        return None

    if amount <= 0:
        return None

    return {
        "rounds": rounds,
        "game": game,
        "emoji": GAMES[game],
        "amount": amount
    }


# =========================================================
# GAME DATABASE
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


def user_game(user_id):

    with closing(db()) as con:

        return con.execute("""
        SELECT *
        FROM games
        WHERE status IN ('waiting','playing')
        AND (
            creator=?
            OR opponent=?
        )
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

    if await member_ok(
        context.bot,
        user.id
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی",
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

async def start(update, context):

    user = update.effective_user

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
        reply_markup=main_keyboard(user.id)
    )


# =========================================================
# GAMES MENU
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
        "یکی از حالت‌ها را انتخاب کن.",
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
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "مثال تعداد بالا:\n"
        "20 تاس 1\n"
        "100 دارت 0.5\n\n"
        "تعداد دور محدود نیست."
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
        "4 تاس 0.1\n\n"
        "بازیکن دوم روی «ورود به بازی» می‌زند.\n\n"
        "در این حالت:\n"
        "👤 سازنده ایموجی می‌فرستد\n"
        "👤 حریف ایموجی می‌فرستد\n"
        "🤖 ربات هیچ پرتابی انجام نمی‌دهد."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "هر چهار بازی دقیقاً یک منطق دارند:\n\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "مثلاً برای بسکتبال:\n\n"
        "👤 کاربر 🏀\n"
        "🤖 ربات 🏀\n"
        "👤 کاربر 🏀\n"
        "🤖 ربات 🏀\n\n"
        "و همین ترتیب تا پایان دورها ادامه دارد."
    )


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(update, context):

    msg = update.message

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

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    existing = user_game(
        user.id
    )

    if existing:

        await msg.reply_text(
            "⏳ شما در حال حاضر داخل یک بازی هستید."
        )

        return

    game_id = secrets.token_hex(16)

    with closing(db()) as con:

        con.execute("""
        INSERT INTO games (
            id,
            chat_id,
            creator,
            game,
            emoji,
            rounds,
            mode,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            game_id,
            msg.chat.id,
            user.id,
            parsed["game"],
            parsed["emoji"],
            parsed["rounds"],
            "friend",
            "waiting"
        ))

        con.commit()

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
        f"{parsed['emoji']} بازی ساخته شد!\n\n"
        f"👤 سازنده: {user.full_name}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 تعداد دور: {parsed['rounds']}\n"
        f"💰 اعتبار: {money(parsed['amount'])}\n\n"
        "اعتبار این بازی داخلی است.",
        reply_markup=keyboard
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    user = q.from_user

    register(user)

    if user.id == game["creator"]:

        await q.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

    if user_game(user.id):

        await q.answer(
            "❌ شما در یک بازی دیگر هستید.",
            show_alert=True
        )

        return

    with closing(db()) as con:

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

        con.commit()

    await q.answer(
        "✅ وارد بازی شدی."
    )

    creator_name = get_user_name(
        game["creator"]
    )

    opponent_name = get_user_name(
        user.id
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی شروع شد!\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"👤 حریف: {opponent_name}\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دورها: {game['rounds']}\n\n"
        f"نوبت {creator_name} است."
    )


# =========================================================
# ROBOT GAME
# =========================================================

async def robot_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if q.from_user.id != game["creator"]:

        await q.answer(
            "❌ فقط سازنده می‌تواند انتخاب کند.",
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

        con.commit()

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی با ربات شروع شد!\n\n"
        f"👤 سازنده: {get_user_name(game['creator'])}\n"
        f"🤖 حریف: ربات\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دورها: {game['rounds']}\n\n"
        f"👤 اول شما {game['emoji']} را بفرست.\n"
        f"🤖 بعد از آن ربات {game['emoji']} را می‌اندازد."
    )


# =========================================================
# CANCEL
# =========================================================

async def cancel_game(update, context):

    q = update.callback_query

    game_id = q.data.split(
        ":",
        1
    )[1]

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

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='cancelled'
        WHERE id=?
        AND status='waiting'
        """, (
            game_id,
        ))

        con.commit()

    await q.answer(
        "لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد."
    )


# =========================================================
# DICE / DART / BASKETBALL / BOWLING
# =========================================================

async def game_emoji_handler(update, context):

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

    if emoji not in EMOJI_TO_GAME:
        return

    user = update.effective_user

    register(user)

    # -----------------------------------------------------
    # پیدا کردن بازی فعال همین کاربر
    # -----------------------------------------------------

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

    value = int(
        dice.value
    )

    # =====================================================
    # ROBOT MODE
    # =====================================================

    if game["mode"] == "robot":

        # فقط سازنده می‌تواند بازی کند
        if user.id != game["creator"]:
            return

        # اگر تعداد دور تمام شده
        if game["creator_round"] >= game["rounds"]:
            return

        # اگر قبلاً کاربر بازی کرده و ربات هنوز جواب نداده
        if game["creator_round"] > game["opponent_round"]:

            await msg.reply_text(
                "⏳ هنوز نوبت ربات است."
            )

            return

        # -------------------------------------------------
        # ثبت امتیاز کاربر
        # -------------------------------------------------

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                creator_round=creator_round+1,
                creator_score=creator_score+?
            WHERE id=?
            AND status='playing'
            """, (
                value,
                game["id"]
            ))

            con.commit()

        creator_name = get_user_name(
            game["creator"]
        )

        await msg.reply_text(
            f"👤 {creator_name}: {value}\n\n"
            f"🤖 نوبت ربات..."
        )

        # =================================================
        # مهم:
        # برای هر چهار بازی ربات همان emoji را می‌اندازد
        # =================================================

        bot_result = await context.bot.send_dice(
            chat_id=msg.chat.id,
            emoji=emoji
        )

        bot_value = int(
            bot_result.dice.value
        )

        # -------------------------------------------------
        # ثبت امتیاز ربات
        # -------------------------------------------------

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                opponent_round=opponent_round+1,
                opponent_score=opponent_score+?
            WHERE id=?
            AND status='playing'
            """, (
                bot_value,
                game["id"]
            ))

            con.commit()

        await msg.reply_text(
            f"🤖 ربات: {bot_value}"
        )

        current = get_game(
            game["id"]
        )

        # -------------------------------------------------
        # پایان بازی
        # -------------------------------------------------

        if (
            current["creator_round"] >= current["rounds"]
            and
            current["opponent_round"] >= current["rounds"]
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

    # -----------------------------------------------------
    # سازنده
    # -----------------------------------------------------

    if user.id == game["creator"]:

        if game["creator_round"] >= game["rounds"]:
            return

        # اگر سازنده قبلاً زده، نوبت حریف است
        if game["creator_round"] > game["opponent_round"]:

            await msg.reply_text(
                "⏳ منتظر پرتاب حریف هستیم."
            )

            return

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                creator_round=creator_round+1,
                creator_score=creator_score+?
            WHERE id=?
            AND status='playing'
            """, (
                value,
                game["id"]
            ))

            con.commit()

        await msg.reply_text(
            f"👤 {get_user_name(user.id)}: {value}\n\n"
            f"⏳ نوبت {get_user_name(game['opponent'])}."
        )

    # -----------------------------------------------------
    # حریف
    # -----------------------------------------------------

    elif user.id == game["opponent"]:

        if game["opponent_round"] >= game["rounds"]:
            return

        # تا سازنده بازی نکرده، حریف نباید بازی کند
        if game["creator_round"] <= game["opponent_round"]:

            await msg.reply_text(
                "⏳ هنوز نوبت شما نیست."
            )

            return

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET
                opponent_round=opponent_round+1,
                opponent_score=opponent_score+?
            WHERE id=?
            AND status='playing'
            """, (
                value,
                game["id"]
            ))

            con.commit()

        await msg.reply_text(
            f"👤 {get_user_name(user.id)}: {value}"
        )

    else:
        return

    current = get_game(
        game["id"]
    )

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
# FINISH GAME
# =========================================================

async def finish_game(game_id, context):

    game = get_game(
        game_id
    )

    if not game:
        return

    creator_score = int(
        game["creator_score"]
    )

    opponent_score = int(
        game["opponent_score"]
    )

    creator_name = get_user_name(
        game["creator"]
    )

    if game["mode"] == "robot":

        opponent_name = "ربات"

        if creator_score > opponent_score:

            result = (
                f"🏆 {creator_name} برنده شد!"
            )

        elif creator_score < opponent_score:

            result = (
                "🤖 ربات برنده شد!"
            )

        else:

            result = (
                "🤝 بازی مساوی شد!"
            )

    else:

        opponent_name = get_user_name(
            game["opponent"]
        )

        if creator_score > opponent_score:

            result = (
                f"🏆 {creator_name} برنده شد!"
            )

        elif creator_score < opponent_score:

            result = (
                f"🏆 {opponent_name} برنده شد!"
            )

        else:

            result = (
                "🤝 بازی مساوی شد!"
            )

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='finished'
        WHERE id=?
        AND status='playing'
        """, (
            game_id,
        ))

        con.commit()

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"{game['emoji']} نتیجه نهایی\n\n"
            f"👤 سازنده: {creator_name}\n"
            f"🤝 حریف: {opponent_name}\n\n"
            f"🎮 بازی: {game['game']}\n"
            f"🔢 دورها: {game['rounds']}\n\n"
            f"👤 {creator_name}: {creator_score}\n"
            f"🤖/👤 {opponent_name}: {opponent_score}\n\n"
            f"{result}"
        )
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        "💰 موجودی\n\n"
        f"{money(balance(q.from_user.id))} اعتبار"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال\n\n"
        "روی پیام گیرنده Reply کن و بنویس:\n\n"
        "انتقال 1"
    )


async def transfer_handler(update, context):

    msg = update.message

    if not msg:
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    text = digits(
        msg.text.strip()
    )

    parts = text.split()

    if len(parts) != 2:
        return

    if parts[0] != "انتقال":
        return

    try:

        amount = float(
            parts[1]
        )

    except ValueError:

        await msg.reply_text(
            "❌ مبلغ اشتباه است."
        )

        return

    if amount <= 0:
        return

    sender = update.effective_user

    receiver = (
        msg.reply_to_message.from_user
    )

    if not receiver:
        return

    if sender.id == receiver.id:

        await msg.reply_text(
            "❌ انتقال به خودت امکان‌پذیر نیست."
        )

        return

    register(sender)
    register(receiver)

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            sender_row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                sender.id,
            )).fetchone()

            if not sender_row:

                con.rollback()

                await msg.reply_text(
                    "❌ کاربر پیدا نشد."
                )

                return

            sender_balance = float(
                sender_row["balance"]
            )

            if sender_balance < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                sender.id
            ))

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

            log.exception(
                "transfer error"
            )

            await msg.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💰 مقدار: {money(amount)} اعتبار\n"
        f"👤 گیرنده: {receiver.full_name}"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "این بخش در این نسخه فعال نیست."
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
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "کاربر ایموجی را می‌فرستد.\n"
        "بعد ربات همان ایموجی را می‌اندازد.\n\n"
        "👥 بازی دوستان:\n"
        "هر دو بازیکن خودشان ایموجی می‌فرستند.\n\n"
        "🔄 انتقال:\n"
        "روی پیام کاربر Reply کن:\n"
        "انتقال 0.1"
    )


# =========================================================
# ADMIN KEYBOARD
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش",
                callback_data="admin_add"
            ),
            InlineKeyboardButton(
                "➖ کاهش",
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
# ADMIN
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
        "👑 پنل مالک",
        reply_markup=admin_keyboard()
    )


async def admin_add(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin"] = "add"

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

    context.user_data["admin"] = "remove"

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

    context.user_data["admin"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


async def admin_stats(update, context):

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

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games_count}\n"
        f"⏳ فعال: {active}"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def admin_text(update, context):

    user = update.effective_user

    if user.id != OWNER_ID:
        return

    action = context.user_data.get(
        "admin"
    )

    if not action:
        return

    text = digits(
        update.message.text.strip()
    )

    # -----------------------------------------------------
    # BALANCE
    # -----------------------------------------------------

    if action == "balance":

        try:

            target = int(text)

        except ValueError:

            await update.message.reply_text(
                "❌ آیدی اشتباه است."
            )

            return

        await update.message.reply_text(
            "💰 موجودی:\n\n"
            f"{money(balance(target))} اعتبار"
        )

        context.user_data.pop(
            "admin",
            None
        )

        return

    # -----------------------------------------------------
    # ADD / REMOVE
    # -----------------------------------------------------

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ"
        )

        return

    try:

        target = int(
            parts[0]
        )

        amount = float(
            parts[1]
        )

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار اشتباه است."
        )

        return

    if amount <= 0:
        return

    if action == "add":

        ok = add_balance(
            target,
            amount
        )

    else:

        ok = add_balance(
            target,
            -amount
        )

    if ok:

        await update.message.reply_text(
            "✅ انجام شد.\n\n"
            f"💰 موجودی جدید: "
            f"{money(balance(target))} اعتبار"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

    context.user_data.pop(
        "admin",
        None
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(update, context):

    data = update.callback_query.data

    if data == "membership":

        q = update.callback_query

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
        await friends(update, context)

    elif data == "robot_help":
        await robot_help(update, context)

    elif data == "balance":
        await balance_button(update, context)

    elif data == "transfer":
        await transfer_button(update, context)

    elif data == "withdraw":
        await withdraw(update, context)

    elif data == "help":
        await help_button(update, context)

    elif data == "admin":
        await admin(update, context)

    elif data == "admin_add":
        await admin_add(update, context)

    elif data == "admin_remove":
        await admin_remove(update, context)

    elif data == "admin_balance":
        await admin_balance(update, context)

    elif data == "admin_stats":
        await admin_stats(update, context)

    elif data == "home":

        q = update.callback_query

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
# =========================================================

async def text_router(update, context):

    msg = update.message

    if not msg:
        return

    user = update.effective_user

    register(user)

    text = digits(
        msg.text.strip()
    )

    # -----------------------------------------------------
    # ADMIN
    # -----------------------------------------------------

    if (
        user.id == OWNER_ID
        and context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # TRANSFER
    # -----------------------------------------------------

    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # GAME
    # -----------------------------------------------------

    parsed = parse_game(text)

    if parsed:

        await create_game(
            update,
            context
        )

        return


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    log.error(
        "BOT ERROR: %s",
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

    # دکمه‌ها
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # 🎲 🎯 🏀 🎳
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_emoji_handler
        )
    )

    # متن‌ها
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
        "BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
