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

        # -------------------------------------------------
        # برای دیتابیس قدیمی
        # اگر ستون amount وجود نداشته باشد اضافه می‌شود.
        # -------------------------------------------------

        columns = con.execute("""
        PRAGMA table_info(games)
        """).fetchall()

        names = {
            row["name"]
            for row in columns
        }

        if "amount" not in names:

            con.execute("""
            ALTER TABLE games
            ADD COLUMN amount REAL DEFAULT 0
            """)

        con.commit()


# =========================================================
# USERS
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


def get_name(user_id):

    if not user_id:
        return "ربات"

    row = get_user(user_id)

    if not row:
        return "کاربر"

    name = row["name"] or ""

    username = row["username"] or ""

    if username:

        return f"{name} (@{username})"

    return name or "کاربر"


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


def take_balance(user_id, amount):

    amount = float(amount)

    if amount <= 0:
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

            current = float(
                row["balance"]
            )

            if current < amount:

                con.rollback()

                return False

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                user_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()

            log.exception(
                "take balance error"
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

        m = await bot.get_chat_member(
            CHANNEL,
            user_id
        )

        return m.status in (
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
        reply_markup=keyboard(user.id)
    )


# =========================================================
# HOME
# =========================================================

async def home(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=keyboard(
            q.from_user.id
        )
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
        "چهار بازی فعال است:\n\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "فرمت بازی برای همه یکی است:\n"
        "تعداد بازی نوع بازی مبلغ",
        reply_markup=kb
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
        "مثال با تعداد بیشتر:\n"
        "20 تاس 1\n"
        "100 دارت 0.5\n\n"
        "تعداد دور محدودیتی از طرف فرمت بازی ندارد."
    )


# =========================================================
# FRIENDS INFO
# =========================================================

async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "بعد از ساخت بازی، بازیکن دوم "
        "روی «ورود به بازی» می‌زند.\n\n"
        "سپس:\n"
        "👤 بازیکن اول ایموجی بازی را می‌فرستد.\n"
        "👤 بازیکن دوم ایموجی بازی را می‌فرستد.\n\n"
        "🤖 ربات در بازی دوستان هیچ ایموجی‌ای "
        "پرتاب نمی‌کند."
    )


# =========================================================
# ROBOT INFO
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "ترتیب:\n\n"
        "👤 شما 🎲\n"
        "🤖 ربات 🎲\n"
        "👤 شما 🎲\n"
        "🤖 ربات 🎲\n\n"
        "برای 🎯 🏀 🎳 هم همین منطق است.\n\n"
        "یعنی اول همیشه پرتاب کاربر ثبت می‌شود، "
        "بعد ربات پرتاب می‌کند."
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
            "❌ شما در حال حاضر یک بازی فعال دارید."
        )

        return

    amount = parsed["amount"]

    # -----------------------------------------------------
    # مبلغ سازنده از موجودی داخلی کم می‌شود.
    # -----------------------------------------------------

    if not take_balance(
        user.id,
        amount
    ):

        await msg.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {money(balance(user.id))} TRX\n"
            f"💵 شرط: {money(amount)} TRX"
        )

        return

    game_id = secrets.token_hex(16)

    with closing(db()) as con:

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
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
        """, (
            game_id,
            msg.chat.id,
            user.id,
            parsed["game"],
            parsed["emoji"],
            parsed["rounds"],
            amount,
            "friend",
            "waiting"
        ))

        con.commit()

    creator_name = get_name(
        user.id
    )

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
        f"{parsed['emoji']} بازی ساخته شد.\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 تعداد دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "بازیکن دوم می‌تواند وارد شود.",
        reply_markup=kb
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

    game = get_game(
        game_id
    )

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
            "❌ این بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

    old = user_game(
        user.id
    )

    if old:

        await q.answer(
            "❌ شما در حال بازی دیگری هستی.",
            show_alert=True
        )

        return

    amount = float(
        game["amount"]
    )

    if not take_balance(
        user.id,
        amount
    ):

        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

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

            add_balance(
                user.id,
                amount
            )

            await q.answer(
                "❌ بازی دیگر در دسترس نیست.",
                show_alert=True
            )

            return

        con.commit()

    creator_name = get_name(
        game["creator"]
    )

    opponent_name = get_name(
        user.id
    )

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی شروع شد!\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"👤 حریف: {opponent_name}\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دورها: {game['rounds']}\n"
        f"💰 شرط هر نفر: {money(amount)} TRX\n\n"
        f"👤 نوبت {creator_name}\n"
        f"{game['emoji']} ایموجی بازی را بفرست."
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
            "❌ فقط سازنده می‌تواند این گزینه را بزند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

    amount = float(
        game["amount"]
    )

    with closing(db()) as con:

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
                "❌ بازی قابل شروع نیست.",
                show_alert=True
            )

            return

        con.commit()

    await q.answer()

    creator_name = get_name(
        game["creator"]
    )

    await q.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"👤 بازیکن: {creator_name}\n"
        f"🤖 حریف: ربات\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 تعداد دور: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        f"👤 نوبت شماست.\n"
        f"{game['emoji']} ایموجی بازی را بفرست.\n\n"
        "بعد از ثبت پرتاب شما، ربات پرتاب می‌کند."
    )


# =========================================================
# CANCEL GAME
# =========================================================

async def cancel_game(update, context):

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
            "❌ این بازی دیگر قابل لغو نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        cur = con.execute("""
        UPDATE games
        SET status='cancelled'
        WHERE id=?
        AND status='waiting'
        """, (
            game_id,
        ))

        con.commit()

    if cur.rowcount == 1:

        add_balance(
            game["creator"],
            float(game["amount"])
        )

    await q.answer(
        "لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(game['amount'])} TRX "
        "به موجودی سازنده برگشت."
    )


# =========================================================
# CHECK TURN
# =========================================================

def creator_turn(game):

    return (
        int(game["creator_round"])
        ==
        int(game["opponent_round"])
    )


def opponent_turn(game):

    return (
        int(game["creator_round"])
        >
        int(game["opponent_round"])
    )


# =========================================================
# DICE / GAME HANDLER
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

    register(user)

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

        if user.id != game["creator"]:
            return

        # کاربر فقط وقتی اجازه دارد که
        # تعداد پرتاب او و ربات برابر باشد.
        if not creator_turn(game):

            await msg.reply_text(
                "⏳ هنوز نوبت ربات است."
            )

            return

        if int(game["creator_round"]) >= int(game["rounds"]):

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
            f"👤 {get_name(user.id)}: {value}\n\n"
            "🤖 نوبت ربات..."
        )

        # -------------------------------------------------
        # فقط در Robot Mode ربات پرتاب می‌کند.
        # -------------------------------------------------

        try:

            bot_message = await context.bot.send_dice(
                chat_id=msg.chat.id,
                emoji=emoji
            )

            bot_value = int(
                bot_message.dice.value
            )

        except Exception:

            log.exception(
                "robot send dice failed"
            )

            await msg.reply_text(
                "❌ پرتاب ربات انجام نشد."
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

        if (
            int(current["creator_round"])
            >= int(current["rounds"])
            and
            int(current["opponent_round"])
            >= int(current["rounds"])
        ):

            await finish(
                current["id"],
                context
            )

        else:

            current = get_game(
                game["id"]
            )

            left = (
                int(current["rounds"])
                -
                int(current["creator_round"])
            )

            await msg.reply_text(
                f"👤 نوبت بعدی شماست.\n"
                f"🔢 دور باقی‌مانده: {left}\n"
                f"{emoji} ایموجی بازی را بفرست."
            )

        return

    # =====================================================
    # FRIEND MODE
    # =====================================================

    if game["mode"] != "friend":
        return

    creator = int(
        game["creator"]
    )

    opponent = int(
        game["opponent"]
    )

    # -----------------------------------------------------
    # CREATOR
    # -----------------------------------------------------

    if user.id == creator:

        if int(game["creator_round"]) >= int(game["rounds"]):

            return

        if not creator_turn(game):

            await msg.reply_text(
                f"⏳ هنوز نوبت {get_name(opponent)} است."
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
            f"👤 {get_name(creator)}: {value}\n\n"
            f"⏳ نوبت {get_name(opponent)}"
        )

    # -----------------------------------------------------
    # OPPONENT
    # -----------------------------------------------------

    elif user.id == opponent:

        if int(game["opponent_round"]) >= int(game["rounds"]):

            return

        if not opponent_turn(game):

            await msg.reply_text(
                f"⏳ هنوز نوبت {get_name(creator)} است."
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
            f"👤 {get_name(opponent)}: {value}"
        )

    else:

        return

    # -----------------------------------------------------
    # CHECK FINISH
    # -----------------------------------------------------

    current = get_game(
        game["id"]
    )

    if (
        int(current["creator_round"])
        >= int(current["rounds"])
        and
        int(current["opponent_round"])
        >= int(current["rounds"])
    ):

        await finish(
            current["id"],
            context
        )


# =========================================================
# FINISH GAME
# =========================================================

async def finish(game_id, context):

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

    amount = float(
        game["amount"]
    )

    pot = round(
        amount * 2,
        8
    )

    # -----------------------------------------------------
    # فقط یک بار پایان بازی ثبت شود.
    # -----------------------------------------------------

    with closing(db()) as con:

        cur = con.execute("""
        UPDATE games
        SET status='finished'
        WHERE id=?
        AND status='playing'
        """, (
            game_id,
        ))

        con.commit()

    if cur.rowcount != 1:
        return

    creator_name = get_name(
        game["creator"]
    )

    if game["mode"] == "robot":

        # -------------------------------------------------
        # ROBOT
        # -------------------------------------------------

        if creator_score > opponent_score:

            add_balance(
                game["creator"],
                pot
            )

            result = (
                f"🏆 {creator_name} برنده شد!"
            )

            prize_text = (
                f"💰 برد: {money(pot)} TRX"
            )

        elif creator_score < opponent_score:

            result = (
                "🤖 ربات برنده شد."
            )

            prize_text = (
                f"💰 مبلغ بازی: {money(pot)} TRX"
            )

        else:

            add_balance(
                game["creator"],
                amount
            )

            result = (
                "🤝 بازی مساوی شد."
            )

            prize_text = (
                f"💰 شرط شما برگشت داده شد: "
                f"{money(amount)} TRX"
            )

        text = (
            f"{game['emoji']} نتیجه بازی\n\n"
            f"👤 سازنده: {creator_name}\n"
            f"🤖 حریف: ربات\n\n"
            f"🎮 بازی: {game['game']}\n"
            f"🔢 تعداد دور: {game['rounds']}\n"
            f"💰 شرط: {money(amount)} TRX\n\n"
            f"📊 امتیازها:\n"
            f"👤 {creator_name}: {creator_score}\n"
            f"🤖 ربات: {opponent_score}\n\n"
            f"{result}\n"
            f"{prize_text}"
        )

    else:

        # -------------------------------------------------
        # FRIEND
        # -------------------------------------------------

        opponent_name = get_name(
            game["opponent"]
        )

        if creator_score > opponent_score:

            add_balance(
                game["creator"],
                pot
            )

            result = (
                f"🏆 {creator_name} برنده شد!"
            )

            winner_name = creator_name

            prize_text = (
                f"💰 {winner_name} "
                f"{money(pot)} TRX برد."
            )

        elif creator_score < opponent_score:

            add_balance(
                game["opponent"],
                pot
            )

            result = (
                f"🏆 {opponent_name} برنده شد!"
            )

            winner_name = opponent_name

            prize_text = (
                f"💰 {winner_name} "
                f"{money(pot)} TRX برد."
            )

        else:

            add_balance(
                game["creator"],
                amount
            )

            add_balance(
                game["opponent"],
                amount
            )

            result = (
                "🤝 بازی مساوی شد."
            )

            prize_text = (
                f"💰 مبلغ هر دو بازیکن "
                f"({money(amount)} TRX) برگشت داده شد."
            )

        text = (
            f"{game['emoji']} نتیجه بازی\n\n"
            f"👤 سازنده: {creator_name}\n"
            f"👤 حریف: {opponent_name}\n\n"
            f"🎮 بازی: {game['game']}\n"
            f"🔢 تعداد دور: {game['rounds']}\n"
            f"💰 شرط هر نفر: {money(amount)} TRX\n"
            f"🏦 مجموع شرط: {money(pot)} TRX\n\n"
            f"📊 امتیازها:\n"
            f"👤 {creator_name}: {creator_score}\n"
            f"👤 {opponent_name}: {opponent_score}\n\n"
            f"{result}\n"
            f"{prize_text}"
        )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# =========================================================
# BALANCE BUTTON
# =========================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        f"💰 موجودی {get_name(q.from_user.id)}:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# BALANCE COMMAND IN GROUP
# =========================================================

async def balance_command(update, context):

    user = update.effective_user

    register(user)

    await update.message.reply_text(
        f"💰 موجودی {get_name(user.id)}:\n\n"
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
        "انتقال 1\n\n"
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

    text = digits(
        msg.text.strip()
    )

    m = re.fullmatch(
        r"انتقال\s+(\d+(?:\.\d+)?)",
        text
    )

    if not m:

        await msg.reply_text(
            "❌ فرمت اشتباه است.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    amount = float(
        m.group(1)
    )

    if amount <= 0:

        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    sender = update.effective_user

    receiver = (
        msg.reply_to_message.from_user
    )

    if not receiver:
        return

    if receiver.is_bot:

        await msg.reply_text(
            "❌ نمی‌توان به ربات انتقال داد."
        )

        return

    if sender.id == receiver.id:

        await msg.reply_text(
            "❌ انتقال به خودت ممکن نیست."
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

            receiver_row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                receiver.id,
            )).fetchone()

            if not sender_row or not receiver_row:

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
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{money(sender_balance)} TRX\n"
                    f"💵 مبلغ: "
                    f"{money(amount)} TRX"
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
        f"👤 فرستنده: {get_name(sender.id)}\n"
        f"👤 گیرنده: {get_name(receiver.id)}\n"
        f"💰 مقدار: {money(amount)} TRX\n\n"
        f"💳 موجودی جدید فرستنده: "
        f"{money(balance(sender.id))} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "این بخش فعلاً فعال نیست."
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
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام کاربر Reply کن:\n"
        "انتقال 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر ایموجی می‌فرستد، "
        "بعد ربات همان بازی را می‌اندازد.\n\n"
        "👥 بازی دوستان:\n"
        "فقط کاربران ایموجی بازی را می‌فرستند.\n\n"
        "ℹ️ موجودی TRX نمایش‌داده‌شده "
        "اعتبار داخلی این بات است و TRX واقعی نیست."
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


# =========================================================
# ADMIN ADD
# =========================================================

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


# =========================================================
# ADMIN REMOVE
# =========================================================

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


# =========================================================
# ADMIN BALANCE
# =========================================================

async def admin_balance(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin"] = "balance"

    await q.message.reply_text(
        "🆔 آیدی عددی کاربر را بفرست."
    )


# =========================================================
# ADMIN STATS
# =========================================================

async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    with closing(db()) as con:

        users = con.execute("""
        SELECT COUNT(*) c
        FROM users
        """).fetchone()["c"]

        games_count = con.execute("""
        SELECT COUNT(*) c
        FROM games
        """).fetchone()["c"]

        active = con.execute("""
        SELECT COUNT(*) c
        FROM games
        WHERE status IN ('waiting','playing')
        """).fetchone()["c"]

        total_balance = con.execute("""
        SELECT COALESCE(SUM(balance), 0) total
        FROM users
        """).fetchone()["total"]

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

async def admin_text(update, context):

    if update.effective_user.id != OWNER_ID:
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
            f"👤 {get_name(target)}\n\n"
            f"💰 موجودی:\n"
            f"{money(balance(target))} TRX"
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

        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    # کاربر باید قبلاً ثبت شده باشد
    if not get_user(target):

        await update.message.reply_text(
            "❌ این کاربر هنوز در دیتابیس ثبت نشده است."
        )

        context.user_data.pop(
            "admin",
            None
        )

        return

    if action == "add":

        ok = add_balance(
            target,
            amount
        )

        action_text = "افزایش"

    else:

        ok = add_balance(
            target,
            -amount
        )

        action_text = "کاهش"

    if ok:

        await update.message.reply_text(
            f"✅ {action_text} انجام شد.\n\n"
            f"👤 کاربر: {get_name(target)}\n"
            f"💰 مقدار: {money(amount)} TRX\n"
            f"💳 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد.\n"
            "ممکن است موجودی برای کاهش کافی نباشد."
        )

    context.user_data.pop(
        "admin",
        None
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback(update, context):

    q = update.callback_query

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

        await home(
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
    # BALANCE
    # -----------------------------------------------------

    if text == "موجودی":

        await balance_command(
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
        context.error
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

    # -----------------------------------------------------
    # START
    # -----------------------------------------------------

    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # -----------------------------------------------------
    # CALLBACKS
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # -----------------------------------------------------
    # GAME EMOJIS
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # -----------------------------------------------------
    # TEXT
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router
        )
    )

    # -----------------------------------------------------
    # ERROR
    # -----------------------------------------------------

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
