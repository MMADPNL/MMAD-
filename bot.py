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


def add_balance(user_id, amount):

    amount = float(amount)

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
            log.exception("add_balance")

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
# VALID GAME VALUE
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

    text = digits(
        text.strip()
    )

    pattern = (
        r"^(\d{1,3})\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d{1,8}(?:\.\d{1,8})?)$"
    )

    m = re.fullmatch(
        pattern,
        text
    )

    if not m:
        return None

    try:

        rounds = int(m.group(1))
        amount = float(m.group(3))

    except Exception:

        return None

    if rounds < 1 or rounds > 100:
        return None

    if amount <= 0 or amount > 1000000:
        return None

    game = m.group(2)

    return {
        "rounds": rounds,
        "game": game,
        "emoji": GAMES[game],
        "amount": round(amount, 8)
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

    user = update.effective_user

    if not user or not update.message:
        return

    register(user)

    if not await membership(
        update,
        context
    ):
        return

    # پاک کردن حالت قبلی پنل
    context.user_data.pop("admin_action", None)

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "واحد موجودی: TRX داخلی بات\n"
        "بلاکچین و انتقال واقعی فعال نیست.\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=main_keyboard(
            user.id
        )
    )


# =========================================================
# GAMES MENU
# =========================================================

async def games_menu(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎮 بازی‌ها\n\n"
        "برای ساخت بازی در گپ بنویس:\n\n"
        "3 تاس 100\n"
        "3 دارت 100\n"
        "3 بسکتبال 100\n"
        "3 بولینگ 100\n\n"
        "🤖 بازی با ربات:\n"
        "اول تمام پرتاب‌های کاربر انجام می‌شود، "
        "بعد تمام پرتاب‌های ربات."
    )


# =========================================================
# EXAMPLES
# =========================================================

async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎯 مثال:\n\n"
        "3 تاس 100\n"
        "3 دارت 100\n"
        "3 بسکتبال 100\n"
        "3 بولینگ 100\n\n"
        "عدد اول = تعداد پرتاب\n"
        "عدد آخر = مبلغ TRX داخلی"
    )


# =========================================================
# HELP
# =========================================================

async def help_menu(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی در گپ:\n"
        "3 تاس 100\n\n"
        "💰 موجودی:\n"
        "موجودی\n\n"
        "🔄 انتقال:\n"
        "روی پیام گیرنده Reply کن و بنویس:\n"
        "انتقال 100\n\n"
        "🤖 بازی با ربات:\n"
        "کاربر اول همه پرتاب‌ها را انجام می‌دهد.\n"
        "بعد ربات همه پرتاب‌ها را انجام می‌دهد.\n"
        "سپس نتیجه اعلام می‌شود."
    )


# =========================================================
# FRIEND
# =========================================================

async def friends_menu(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "3 تاس 100\n\n"
        "بازیکن دوم روی «ورود به بازی» می‌زند.\n\n"
        "سپس هر دو بازیکن به نوبت پرتاب می‌کنند."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "برای مثال:\n"
        "3 تاس 100\n\n"
        "ترتیب:\n"
        "1️⃣ کاربر پرتاب اول\n"
        "2️⃣ کاربر پرتاب دوم\n"
        "3️⃣ کاربر پرتاب سوم\n"
        "4️⃣ ربات پرتاب اول\n"
        "5️⃣ ربات پرتاب دوم\n"
        "6️⃣ ربات پرتاب سوم\n"
        "7️⃣ نتیجه\n\n"
        "همین منطق برای هر ۴ بازی است."
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
    # ضد بازی فعال
    # =====================================================

    old = active_user_game(
        user.id
    )

    if old:

        await msg.reply_text(
            "❌ شما یک بازی فعال دارید.\n\n"
            "اول بازی فعلی را تمام کنید."
        )

        return

    amount = parsed["amount"]

    game_id = secrets.token_hex(16)

    # =====================================================
    # کسر شرط سازنده
    # =====================================================

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

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

            current = float(
                row["balance"]
            )

            if current < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: {money(current)} TRX\n"
                    f"🎯 شرط: {money(amount)} TRX"
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

            log.exception("create_game")

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
        f"🔢 تعداد پرتاب: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "یکی از گزینه‌ها را انتخاب کن.",
        reply_markup=keyboard
    )


# =========================================================
# JOIN FRIEND
# =========================================================

async def join_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    user = q.from_user

    register(user)

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

    old = active_user_game(
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

            con.execute("BEGIN IMMEDIATE")

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
                    "❌ بازی قبلاً پر شده.",
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
                    f"❌ موجودی کافی نیست.\n"
                    f"نیاز: {money(amount)} TRX",
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
                status='playing',
                mode='friend'
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

            log.exception("join_game")

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
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
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
            "❌ فقط سازنده می‌تواند انتخاب کند.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی دیگر قابل شروع نیست.",
            show_alert=True
        )

        return

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

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
                "❌ خطا.",
                show_alert=True
            )

            return

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"👤 بازیکن: {display_name(user.id)}\n"
        f"🤖 حریف: ربات\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"👤 حالا شما باید تمام "
        f"{game['rounds']} پرتاب را انجام دهید.\n\n"
        "بعد از آخرین پرتاب، ربات خودش همه پرتاب‌ها را انجام می‌دهد."
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
            "❌ بازی شروع شده است.",
            show_alert=True
        )

        return

    amount = float(game["amount"])

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

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

        value = int(
            sent.dice.value
        )

    except Exception:

        log.exception("robot_throw")
        return None

    if not valid_game_value(
        game["emoji"],
        value
    ):
        return None

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

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
            log.exception("robot_score")

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

            creator_score = int(
                game["creator_score"]
            )

            opponent_score = int(
                game["opponent_score"]
            )

            amount = float(
                game["amount"]
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

                winner_text = (
                    f"🏆 برنده: "
                    f"{display_name(game['creator'])}\n"
                    f"💰 دریافتی: "
                    f"{money(payout)} TRX"
                )

            # =================================================
            # OPPONENT WIN
            # =================================================

            elif opponent_score > creator_score:

                if game["mode"] == "friend":

                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        game["opponent"]
                    ))

                    winner_text = (
                        f"🏆 برنده: "
                        f"{display_name(game['opponent'])}\n"
                        f"💰 دریافتی: "
                        f"{money(payout)} TRX"
                    )

                else:

                    winner_text = (
                        "🤖 ربات برنده شد.\n"
                        f"💸 شما {money(amount)} TRX "
                        "را از دست دادید."
                    )

            # =================================================
            # DRAW
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

                winner_text = (
                    "🤝 بازی مساوی شد.\n"
                    f"💰 {money(amount)} TRX "
                    "به کاربر برگشت داده شد."
                )

            con.commit()

        except Exception:

            con.rollback()

            log.exception("finish_game")
            return

    creator_name = display_name(
        game["creator"]
    )

    opponent_name = (
        "🤖 ربات"
        if game["mode"] == "robot"
        else display_name(game["opponent"])
    )

    text = (
        f"{game['emoji']} نتیجه بازی\n\n"
        f"👤 سازنده: {creator_name}\n"
        f"👤 حریف: {opponent_name}\n\n"
        f"🎮 بازی: {game['game']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        f"📊 امتیاز نهایی:\n"
        f"👤 {creator_name}: "
        f"{creator_score}\n"
        f"👤 {opponent_name}: "
        f"{opponent_score}\n\n"
        f"{winner_text}\n\n"
        "✅ بازی تمام شد؛ می‌توانی بازی جدید بسازی."
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# =========================================================
# HANDLE USER GAME THROW
# =========================================================

async def handle_user_throw(
    update,
    context,
    game,
    user,
    value
):

    game_id = game["id"]

    # =====================================================
    # ROBOT MODE
    # =====================================================

    if game["mode"] == "robot":

        if user.id != game["creator"]:
            return

        # -------------------------------
        # ضد پرتاب اضافه
        # -------------------------------

        if game["creator_round"] >= game["rounds"]:

            await update.message.reply_text(
                "⏳ تمام پرتاب‌های شما انجام شده.\n"
                "🤖 حالا نوبت ربات است."
            )

            return

        # -------------------------------
        # ثبت اتمیک
        # -------------------------------

        with closing(db()) as con:

            try:

                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
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

                if current["creator_round"] >= current["rounds"]:

                    con.rollback()

                    await update.message.reply_text(
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
                    game_id
                ))

                if result.rowcount != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "user_robot_throw"
                )

                return

        current = get_game(game_id)

        if not current:
            return

        await update.message.reply_text(
            f"👤 {display_name(user.id)}\n"
            f"🎯 امتیاز این پرتاب: {value}\n\n"
            f"📊 پرتاب شما: "
            f"{current['creator_round']}/"
            f"{current['rounds']}\n"
            f"🔢 مجموع امتیاز شما: "
            f"{current['creator_score']}"
        )

        # =================================================
        # هنوز پرتاب کاربر تمام نشده
        # =================================================

        if current["creator_round"] < current["rounds"]:

            await update.message.reply_text(
                f"👤 هنوز نوبت شماست.\n"
                f"پرتاب "
                f"{current['creator_round'] + 1} "
                f"را انجام بده."
            )

            return

        # =================================================
        # کاربر تمام کرد
        # ربات شروع می‌کند
        # =================================================

        await update.message.reply_text(
            "✅ تمام پرتاب‌های شما انجام شد.\n\n"
            "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
        )

        # =================================================
        # تمام پرتاب‌های ربات
        # =================================================

        while True:

            current = get_game(game_id)

            if not current:
                return

            if current["status"] != "playing":
                return

            if current["opponent_round"] >= current["rounds"]:
                break

            robot_value = await robot_throw(
                game_id,
                context
            )

            if robot_value is None:
                return

            current = get_game(game_id)

            if not current:
                return

            await update.message.reply_text(
                f"🤖 پرتاب ربات "
                f"{current['opponent_round']}/"
                f"{current['rounds']}\n"
                f"🎯 امتیاز: {robot_value}\n"
                f"📊 مجموع ربات: "
                f"{current['opponent_score']}"
            )

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

            await update.message.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        if game["creator_round"] > game["opponent_round"]:

            await update.message.reply_text(
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
                """, (
                    game_id,
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

                if current["creator_round"] > current["opponent_round"]:
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
                    game_id
                ))

                if result.rowcount != 1:
                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend_creator"
                )

                return

        current = get_game(game_id)

        await update.message.reply_text(
            f"👤 {display_name(user.id)}\n"
            f"🎯 امتیاز: {value}\n\n"
            f"⏳ نوبت حریف."
        )

    # =====================================================
    # OPPONENT
    # =====================================================

    elif user.id == game["opponent"]:

        if game["opponent_round"] >= game["rounds"]:

            await update.message.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        if game["creator_round"] <= game["opponent_round"]:

            await update.message.reply_text(
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
                """, (
                    game_id,
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

                if current["creator_round"] <= current["opponent_round"]:
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
                    game_id
                ))

                if result.rowcount != 1:
                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend_opponent"
                )

                return

        await update.message.reply_text(
            f"👤 {display_name(user.id)}\n"
            f"🎯 امتیاز: {value}\n\n"
            "⏳ نوبت بازیکن اول."
        )

    else:

        return

    # =====================================================
    # FINISH FRIEND GAME
    # =====================================================

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
    # پیدا کردن بازی فقط برای همان کاربر
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

    await handle_user_throw(
        update,
        context,
        game,
        user,
        value
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_button(update, context):

    q = update.callback_query

    await q.answer()

    register(q.from_user)

    await q.message.reply_text(
        f"💰 موجودی {q.from_user.full_name}:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


async def balance_text(update, context):

    msg = update.message

    user = update.effective_user

    if not msg or not user:
        return

    register(user)

    await msg.reply_text(
        f"💰 موجودی {user.full_name}:\n\n"
        f"{money(balance(user.id))} TRX"
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
        "انتقال 100"
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

    text = digits(
        msg.text.strip()
    )

    m = re.fullmatch(
        r"^انتقال\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not m:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "انتقال 100"
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

    register(user)
    register(receiver)

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

            log.exception("transfer")

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

async def withdraw_button(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی در این نسخه فعال نیست.\n"
        "موجودی‌ها داخلی و مجازی هستند."
    )


# =========================================================
# ADMIN MENU
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
                "❌ خروج از حالت مدیریت",
                callback_data="admin_cancel"
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

    context.user_data.pop(
        "admin_action",
        None
    )

    await q.message.reply_text(
        "👑 پنل مدیریت\n\n"
        "فقط عملیات انتخاب‌شده از پنل اجرا می‌شود.",
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

    context.user_data["admin_action"] = "add"

    await q.message.reply_text(
        "➕ افزایش موجودی\n\n"
        "فرمت:\n"
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
        return

    await q.answer()

    context.user_data["admin_action"] = "remove"

    await q.message.reply_text(
        "➖ کاهش موجودی\n\n"
        "فرمت:\n"
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
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "💰 موجودی کاربر\n\n"
        "فقط آیدی عددی را بفرست."
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
# ADMIN CANCEL
# =========================================================

async def admin_cancel(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data.pop(
        "admin_action",
        None
    )

    await q.message.reply_text(
        "✅ از حالت مدیریت خارج شدی."
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
    # مهم:
    # اگر کاربر پیام معمولی/بازی/موجودی/انتقال زد،
    # حالت پنل نباید آن را خراب کند.
    # =====================================================

    if (
        parse_game(text)
        or text in ("موجودی", "بالانس", "balance")
        or text.startswith("انتقال ")
    ):
        context.user_data.pop(
            "admin_action",
            None
        )
        return

    # =====================================================
    # BALANCE
    # =====================================================

    if action == "balance":

        if not re.fullmatch(
            r"\d+",
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
            f"💰 {money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None
        )

        return

    # =====================================================
    # ADD / REMOVE
    # =====================================================

    m = re.fullmatch(
        r"^(\d+)\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not m:

        await msg.reply_text(
            "❌ فرمت صحیح:\n"
            "آیدی مبلغ\n\n"
            "مثال:\n"
            "123456789 100"
        )

        return

    target = int(
        m.group(1)
    )

    amount = float(
        m.group(2)
    )

    if amount <= 0:

        await msg.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not get_user(target):

        await msg.reply_text(
            "❌ این کاربر هنوز در دیتابیس ثبت نشده."
        )

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

        await msg.reply_text(
            "✅ انجام شد.\n\n"
            f"🆔 کاربر: {target}\n"
            f"💰 موجودی جدید: "
            f"{money(balance(target))} TRX"
        )

    else:

        await msg.reply_text(
            "❌ عملیات انجام نشد.\n"
            "احتمالاً موجودی برای کاهش کافی نیست."
        )

    context.user_data.pop(
        "admin_action",
        None
    )


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

    if len(text) > 200:
        return

    # =====================================================
    # بازی باید اول بررسی شود
    # تا پنل مدیریت آن را نگیرد.
    # =====================================================

    parsed = parse_game(text)

    if parsed:

        context.user_data.pop(
            "admin_action",
            None
        )

        await create_game(
            update,
            context
        )

        return

    # =====================================================
    # موجودی
    # =====================================================

    if text.lower() in (
        "موجودی",
        "بالانس",
        "balance"
    ):

        context.user_data.pop(
            "admin_action",
            None
        )

        await balance_text(
            update,
            context
        )

        return

    # =====================================================
    # انتقال
    # =====================================================

    if text.startswith("انتقال"):

        context.user_data.pop(
            "admin_action",
            None
        )

        await transfer_handler(
            update,
            context
        )

        return

    # =====================================================
    # ADMIN
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
    # بقیه پیام‌ها نادیده گرفته می‌شوند
    # =====================================================


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
    # MENU
    # =====================================================

    if data == "games":

        await games_menu(
            update,
            context
        )

    elif data == "examples":

        await examples(
            update,
            context
        )

    elif data == "help":

        await help_menu(
            update,
            context
        )

    elif data == "friends":

        await friends_menu(
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

        await withdraw_button(
            update,
            context
        )

    # =====================================================
    # ADMIN
    # =====================================================

    elif data == "admin":

        await admin_menu(
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

    elif data == "admin_cancel":

        await admin_cancel(
            update,
            context
        )

    # =====================================================
    # HOME
    # =====================================================

    elif data == "home":

        context.user_data.pop(
            "admin_action",
            None
        )

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
    # =====================================================

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # =====================================================
    # TELEGRAM GAME DICE
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

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
