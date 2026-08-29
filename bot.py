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

            amount REAL DEFAULT 0,

            mode TEXT NOT NULL,
            status TEXT NOT NULL,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.commit()

        # اگر دیتابیس قدیمی باشد، ستون amount اضافه شود.
        columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        ]

        if "amount" not in columns:
            con.execute("""
            ALTER TABLE games
            ADD COLUMN amount REAL DEFAULT 0
            """)

        con.commit()


# =========================================================
# USERS
# =========================================================

def register(user):
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


def balance(user_id):
    with closing(db()) as con:

        row = con.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            return 0.0

        return float(row["balance"])


def add_balance(user_id, amount):
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
            log.exception("balance update error")
            return False


def money(value):
    return (
        f"{float(value):.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


def display_name(user):
    if not user:
        return "نامشخص"

    if user.full_name:
        return user.full_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def get_user_name(user_id):
    with closing(db()) as con:
        row = con.execute("""
        SELECT name, username
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            return "نامشخص"

        if row["name"]:
            return row["name"]

        if row["username"]:
            return "@" + row["username"]

        return str(user_id)


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

    rounds = int(m.group(1))
    game = m.group(2)
    amount = float(m.group(3))

    if rounds <= 0:
        return None

    if amount <= 0:
        return None

    # برای جلوگیری از بازی‌های بیش از حد بزرگ
    if rounds > 100:
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
        """, (game_id,)).fetchone()


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
        "💰 واحد بازی: TRX\n\n"
        "از دکمه‌های زیر استفاده کن.",
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
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n\n"
        "نوع بازی را انتخاب کن.",
        reply_markup=kb
    )


async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال:\n\n"
        "4 تاس 1\n"
        "4 دارت 1\n"
        "4 بسکتبال 1\n"
        "4 بولینگ 1\n\n"
        "فرمت:\n"
        "تعداد دور + نام بازی + مبلغ TRX"
    )


async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "4 تاس 1\n\n"
        "بعد از ساخت بازی، بازیکن دوم روی "
        "«ورود به بازی» می‌زند.\n\n"
        "هر دو بازیکن خودشان ایموجی بازی "
        "را می‌فرستند.\n\n"
        "🤖 ربات در بازی دوستان پرتاب نمی‌کند."
    )


async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "4 تاس 1\n\n"
        "برای هر ۴ بازی همین ترتیب است:\n\n"
        "👤 کاربر → ایموجی بازی\n"
        "🤖 ربات → همان بازی\n"
        "👤 کاربر → ایموجی بازی\n"
        "🤖 ربات → همان بازی\n\n"
        "تا پایان تعداد دور ادامه دارد."
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

    # شرط از سازنده کم می‌شود.
    if balance(user.id) < amount:
        await msg.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {money(balance(user.id))} TRX\n"
            f"🎯 شرط: {money(amount)} TRX"
        )
        return

    if not add_balance(
        user.id,
        -amount
    ):
        await msg.reply_text(
            "❌ کسر مبلغ انجام نشد."
        )
        return

    game_id = secrets.token_hex(16)

    try:

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
                creator_round,
                opponent_round,
                creator_score,
                opponent_score,
                amount,
                mode,
                status
            )
            VALUES (?, ?, ?, NULL, ?, ?, ?, 0, 0, 0, 0, ?, 'friend', 'waiting')
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

        add_balance(
            user.id,
            amount
        )

        log.exception(
            "create game error"
        )

        await msg.reply_text(
            "❌ ساخت بازی انجام نشد."
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

    creator_name = display_name(user)

    await msg.reply_text(
        f"{parsed['emoji']} بازی ساخته شد!\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "بازیکن دوم وارد شود یا با ربات بازی کن.",
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

    if not await member_ok(
        context.bot,
        user.id
    ):
        await q.answer(
            "❌ ابتدا عضو کانال شوید.",
            show_alert=True
        )
        return

    if user.id == game["creator"]:
        await q.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True
        )
        return

    if game["status"] != "waiting":
        await q.answer(
            "❌ این بازی دیگر منتظر بازیکن نیست.",
            show_alert=True
        )
        return

    old = user_game(
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

    if balance(user.id) < amount:
        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )
        return

    if not add_balance(
        user.id,
        -amount
    ):
        await q.answer(
            "❌ پرداخت شرط انجام نشد.",
            show_alert=True
        )
        return

    with closing(db()) as con:

        con.execute("""
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

        con.commit()

    creator_name = get_user_name(
        game["creator"]
    )

    opponent_name = display_name(
        user
    )

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی شروع شد!\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"👤 حریف: {opponent_name}\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط هر نفر: {money(amount)} TRX\n\n"
        "👤 نوبت سازنده است.\n"
        f"ایموجی {game['emoji']} را بفرست."
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
            "❌ فقط سازنده می‌تواند.",
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

    creator_name = get_user_name(
        game["creator"]
    )

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی شروع شد!\n\n"
        f"👤 بازیکن: {creator_name}\n"
        f"🤖 حریف: ربات\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"👤 نوبت شما.\n"
        f"ایموجی {game['emoji']} را بفرست.\n\n"
        "بعد از پرتاب شما، ربات همان بازی را می‌اندازد."
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
            "❌ فقط سازنده.",
            show_alert=True
        )
        return

    if game["status"] != "waiting":
        await q.answer(
            "❌ این بازی قابل لغو نیست.",
            show_alert=True
        )
        return

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='cancelled'
        WHERE id=?
        AND status='waiting'
        """, (game_id,))

        con.commit()

    # شرط سازنده برگردانده می‌شود.
    add_balance(
        game["creator"],
        float(game["amount"])
    )

    await q.answer(
        "لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(game['amount'])} TRX به موجودی سازنده برگشت."
    )


# =========================================================
# DICE / GAME EMOJI HANDLER
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

    # پیدا کردن بازی فعال مربوط به همین کاربر
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

        # کاربر نباید دوبار پشت سر هم پرتاب کند.
        if (
            game["creator_round"]
            >
            game["opponent_round"]
        ):
            await msg.reply_text(
                "⏳ هنوز نوبت ربات است."
            )
            return

        if (
            game["creator_round"]
            >= game["rounds"]
        ):
            return

        # ثبت پرتاب کاربر
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
            f"🤖 نوبت ربات..."
        )

        # ربات همان ایموجی را می‌اندازد.
        bot_dice = await context.bot.send_dice(
            chat_id=msg.chat.id,
            emoji=emoji
        )

        bot_value = int(
            bot_dice.dice.value
        )

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

    # بازیکن اول
    if user.id == game["creator"]:

        if (
            game["creator_round"]
            >= game["rounds"]
        ):
            return

        # اگر سازنده بیشتر از حریف زده باشد،
        # باید منتظر حریف باشد.
        if (
            game["creator_round"]
            >
            game["opponent_round"]
        ):
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

        creator_name = get_user_name(
            game["creator"]
        )

        opponent_name = get_user_name(
            game["opponent"]
        )

        await msg.reply_text(
            f"👤 {creator_name}: {value}\n"
            f"⏳ نوبت {opponent_name}."
        )

    # بازیکن دوم
    elif user.id == game["opponent"]:

        if (
            game["opponent_round"]
            >= game["rounds"]
        ):
            return

        if (
            game["creator_round"]
            <= game["opponent_round"]
        ):
            await msg.reply_text(
                "⏳ هنوز نوبت سازنده است."
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
            f"👤 {get_user_name(user.id)}: {value}\n"
            "⏳ نوبت سازنده."
        )

    else:
        return

    current = get_game(
        game["id"]
    )

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
# FINISH
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

    # جلوگیری از دوبار تمام شدن بازی
    with closing(db()) as con:

        cur = con.execute("""
        UPDATE games
        SET status='finished'
        WHERE id=?
        AND status='playing'
        """, (game_id,))

        con.commit()

        if cur.rowcount != 1:
            return

    creator_name = get_user_name(
        game["creator"]
    )

    if game["mode"] == "robot":

        opponent_name = "ربات"

        if creator_score > opponent_score:

            # شرط دو طرف = 2 برابر مبلغ
            prize = amount * 2

            add_balance(
                game["creator"],
                prize
            )

            result = (
                f"🏆 {creator_name} برنده شد!\n"
                f"💰 برد: {money(prize)} TRX"
            )

        elif creator_score < opponent_score:

            prize = 0

            result = (
                f"🤖 ربات برنده شد.\n"
                f"💰 برد کاربر: {money(prize)} TRX"
            )

        else:

            # مساوی: شرط کاربر برگردد.
            add_balance(
                game["creator"],
                amount
            )

            result = (
                "🤝 بازی مساوی شد.\n"
                f"💰 {money(amount)} TRX برگشت داده شد."
            )

    else:

        opponent_name = get_user_name(
            game["opponent"]
        )

        total_prize = amount * 2

        if creator_score > opponent_score:

            add_balance(
                game["creator"],
                total_prize
            )

            result = (
                f"🏆 {creator_name} برنده شد!\n"
                f"💰 برد: {money(total_prize)} TRX"
            )

        elif opponent_score > creator_score:

            add_balance(
                game["opponent"],
                total_prize
            )

            result = (
                f"🏆 {opponent_name} برنده شد!\n"
                f"💰 برد: {money(total_prize)} TRX"
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
                "🤝 بازی مساوی شد.\n"
                f"💰 شرط هر دو نفر ({money(amount)} TRX) برگشت داده شد."
            )

    text = (
        f"{game['emoji']} نتیجه بازی\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"🎯 امتیاز: {creator_score}\n\n"
        f"👤 حریف: {opponent_name}\n"
        f"🎯 امتیاز: {opponent_score}\n\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        f"{result}"
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
        "💰 موجودی شما:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# BALANCE IN GROUP
# =========================================================

async def balance_text(update, context):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    user = update.effective_user

    register(user)

    await msg.reply_text(
        f"💰 موجودی {display_name(user)}:\n\n"
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
        "انتقال 1"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_handler(update, context):

    msg = update.message

    if not msg:
        return

    if msg.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
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
            "❌ فرمت درست:\n"
            "انتقال 1"
        )
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )

        return

    try:
        amount = float(
            m.group(1)
        )
    except ValueError:
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
            "❌ انتقال به خودت ممکن نیست."
        )
        return

    # جلوگیری از انتقال به بات
    if receiver.is_bot:
        await msg.reply_text(
            "❌ به ربات نمی‌توان انتقال داد."
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

            current = float(
                sender_row["balance"]
            )

            if current < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: {money(current)} TRX"
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
        f"👤 فرستنده: {display_name(sender)}\n"
        f"👤 گیرنده: {display_name(receiver)}\n"
        f"💰 مقدار: {money(amount)} TRX"
    )


# =========================================================
# WITHDRAW
# =========================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی فعال نیست."
    )


# =========================================================
# HELP
# =========================================================

async def help_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی در گپ:\n"
        "4 تاس 1\n"
        "4 دارت 1\n"
        "4 بسکتبال 1\n"
        "4 بولینگ 1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر ایموجی می‌فرستد،"
        " بعد ربات همان بازی را می‌اندازد.\n\n"
        "👥 بازی دوستان:\n"
        "هر دو بازیکن خودشان ایموجی می‌فرستند.\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام کاربر Reply کن:\n"
        "انتقال 1"
    )


# =========================================================
# ADMIN
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
        f"💰 مجموع موجودی: {money(total_balance)} TRX"
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

    if action == "balance":

        try:
            target = int(text)
        except ValueError:

            await update.message.reply_text(
                "❌ آیدی اشتباه است."
            )

            return

        await update.message.reply_text(
            "💰 موجودی:\n"
            f"{money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin",
            None
        )

        return

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ"
        )

        return

    try:

        target = int(parts[0])
        amount = float(parts[1])

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

    # مالک ممکن است آیدی کاربر را قبلاً نداشته باشد.
    # فقط اگر کاربر ثبت شده باشد تغییر می‌دهیم.
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
            f"👤 آیدی: {target}\n"
            f"💰 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد.\n"
            "ابتدا کاربر باید ربات را /start کرده باشد."
        )

    context.user_data.pop(
        "admin",
        None
    )


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback(update, context):

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
        await games(update, context)

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
            reply_markup=keyboard(
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

    # -----------------------------
    # موجودی در گپ
    # -----------------------------

    if text == "موجودی":

        await balance_text(
            update,
            context
        )

        return

    # -----------------------------
    # پنل مالک
    # -----------------------------

    if (
        user.id == OWNER_ID
        and context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context
        )

        return

    # -----------------------------
    # انتقال
    # -----------------------------

    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # -----------------------------
    # بازی
    # -----------------------------

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

    log.exception(
        "BOT ERROR",
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

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    # Callback
    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # بازی‌های تلگرام
    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # متن
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


if __name__ == "__main__":
    main()
