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

# =========================================================
# LOG
# =========================================================

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

        # -------------------------------------------------
        # سازگاری دیتابیس قدیمی
        # -------------------------------------------------

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

    return max(
        0.0,
        float(row["balance"])
    )


def add_balance(user_id, amount):

    amount = float(amount)

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

            # ضد موجودی منفی
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
                "add_balance error"
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
# GAME SCORE
# =========================================================

# تلگرام برای این ایموجی‌ها عدد 1 تا 6 می‌دهد.
# برای همه بازی‌ها همان value تلگرام به عنوان امتیاز استفاده می‌شود.

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

    # ضد دستور:
    # فقط دقیقاً:
    # تعداد بازی + نوع بازی + مبلغ
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

    if rounds < 1:
        return None

    if rounds > 100:
        return None

    if amount <= 0:
        return None

    if amount > 1000000:
        return None

    game = m.group(2)

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
# ACTIVE GAME
# =========================================================

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
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            user_id,
            user_id
        )).fetchone()


# =========================================================
# CHAT ACTIVE GAME
# =========================================================

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

        # اگر بررسی کانال امکان‌پذیر نبود،
        # بات از کار نمی‌افتد.
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
        reply_markup=keyboard(
            user.id
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
        "برای ساخت بازی داخل گپ بنویس:\n\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1",
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
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "حداکثر تعداد دور: 100\n\n"
        "واحد مبلغ: TRX داخلی بات"
    )


# =========================================================
# FRIEND HELP
# =========================================================

async def friends(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "داخل گپ بنویس:\n\n"
        "4 تاس 0.1\n\n"
        "بعد بازیکن دوم روی «ورود به بازی» می‌زند.\n\n"
        "در این حالت فقط دو کاربر ایموجی بازی "
        "می‌فرستند و ربات هیچ ایموجی بازی "
        "نمی‌اندازد."
    )


# =========================================================
# ROBOT HELP
# =========================================================

async def robot_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "4 تاس 0.1\n\n"
        "ترتیب بازی:\n\n"
        "1️⃣ کاربر پرتاب اول\n"
        "2️⃣ کاربر پرتاب دوم\n"
        "3️⃣ کاربر پرتاب سوم\n"
        "4️⃣ کاربر پرتاب چهارم\n\n"
        "بعد از تمام پرتاب‌های کاربر:\n\n"
        "🤖 ربات تمام پرتاب‌های خودش را انجام می‌دهد.\n\n"
        "این منطق برای:\n"
        "🎲 تاس\n"
        "🎯 دارت\n"
        "🏀 بسکتبال\n"
        "🎳 بولینگ\n"
        "یکسان است."
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

    # -----------------------------------------------------
    # ضد بازی همزمان
    # -----------------------------------------------------

    existing = user_game(
        user.id
    )

    if existing:

        await msg.reply_text(
            "❌ شما یک بازی فعال دارید.\n\n"
            "اول همان بازی را تمام یا لغو کنید."
        )

        return

    amount = parsed["amount"]

    # -----------------------------------------------------
    # قفل کردن مبلغ در همان تراکنش
    # -----------------------------------------------------

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

            current = float(
                row["balance"]
            )

            # ضد موجودی
            if current < amount:

                con.rollback()

                await msg.reply_text(
                    "❌ موجودی کافی نیست.\n\n"
                    f"💰 موجودی: {money(current)} TRX\n"
                    f"🎯 شرط: {money(amount)} TRX"
                )

                return

            # کسر شرط سازنده
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

                await msg.reply_text(
                    "❌ موجودی تغییر کرده؛ دوباره تلاش کنید."
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
            VALUES (
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

    # -----------------------------------------------------
    # دکمه‌ها
    # -----------------------------------------------------

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
        f"👤 سازنده: {display_name(user.id)}\n"
        f"🎮 بازی: {parsed['game']}\n"
        f"🔢 دور: {parsed['rounds']}\n"
        f"💰 شرط: {money(amount)} TRX\n\n"
        "منتظر بازیکن دوم...",
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

    user = q.from_user

    if not user:
        await q.answer()
        return

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
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True
        )

        return

    if user.id == game["creator"]:

        await q.answer(
            "❌ سازنده نمی‌تواند وارد بازی خودش شود.",
            show_alert=True
        )

        return

    # ضد بازی همزمان
    old = user_game(
        user.id
    )

    if old:

        await q.answer(
            "❌ شما یک بازی فعال دیگر دارید.",
            show_alert=True
        )

        return

    amount = float(
        game["amount"]
    )

    # -----------------------------------------------------
    # ورود اتمیک
    # -----------------------------------------------------

    with closing(db()) as con:

        try:

            con.execute(
                "BEGIN IMMEDIATE"
            )

            # دوباره وضعیت را چک می‌کنیم
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

            # موجودی حریف
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
                    "❌ حساب شما پیدا نشد.",
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

            # کسر شرط حریف
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
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            # ثبت حریف
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

            if con.total_changes != 1:

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
                "join error"
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
        f"🎮 بازی شروع شد!\n\n"
        f"👤 سازنده: {display_name(game['creator'])}\n"
        f"👤 حریف: {display_name(user.id)}\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط هر نفر: {money(amount)} TRX\n\n"
        f"👤 {display_name(game['creator'])} "
        f"اول {game['emoji']} را بفرست."
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
            "❌ فقط سازنده می‌تواند با ربات بازی کند.",
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

        except Exception:

            con.rollback()

            await q.answer(
                "❌ خطا در شروع بازی.",
                show_alert=True
            )

            return

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی با ربات شروع شد!\n\n"
        f"👤 بازیکن: {display_name(user.id)}\n"
        f"🤖 حریف: ربات\n"
        f"{game['emoji']} بازی: {game['game']}\n"
        f"🔢 دور: {game['rounds']}\n"
        f"💰 شرط: {money(game['amount'])} TRX\n\n"
        f"👤 اول شما تمام {game['rounds']} "
        f"پرتاب را انجام بده.\n\n"
        "بعد از آخرین پرتاب شما، ربات شروع می‌کند."
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
            "❌ فقط سازنده می‌تواند لغو کند.",
            show_alert=True
        )

        return

    # فقط waiting قابل لغو است
    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده و قابل لغو نیست.",
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
                    "❌ بازی قبلاً تغییر کرده.",
                    show_alert=True
                )

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

                await q.answer(
                    "❌ لغو انجام نشد.",
                    show_alert=True
                )

                return

            # برگشت شرط سازنده
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
                "cancel error"
            )

            await q.answer(
                "❌ خطا در لغو بازی.",
                show_alert=True
            )

            return

    await q.answer(
        "✅ بازی لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(amount)} TRX به موجودی سازنده برگشت."
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
            "robot dice error"
        )

        return None

    if not valid_game_value(
        game["emoji"],
        value
    ):
        return None

    # -----------------------------------------------------
    # ثبت اتمیک
    # -----------------------------------------------------

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

            con.execute("""
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

            if con.total_changes != 1:

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
# FINISH
# =========================================================

async def finish(
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

            # باید هر دو کامل باشند
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

            # -------------------------------------------------
            # جلوگیری از تسویه دوباره
            # -------------------------------------------------

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
                return

            # -------------------------------------------------
            # تسویه
            # -------------------------------------------------

            if creator_score > opponent_score:

                winner = game["creator"]

                payout = amount * 2

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    payout,
                    winner
                ))

                winner_text = (
                    f"🏆 برنده: "
                    f"{display_name(winner)}\n"
                    f"💰 برد: {money(amount)} TRX"
                )

            elif opponent_score > creator_score:

                winner = game["opponent"]

                # در حالت ربات opponent=0 است و
                # نباید موجودی کاربر 0 را تغییر دهیم.
                if game["mode"] == "friend":

                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        winner
                    ))

                else:

                    # در بازی ربات، شرط کاربر برنده
                    # می‌تواند برگردد/دوبرابر شود.
                    payout = amount * 2

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        payout,
                        game["creator"]
                    ))

                if game["mode"] == "robot":

                    winner_text = (
                        "🤖 ربات برنده شد.\n"
                        f"💰 ربات برنده شد: "
                        f"{money(amount)} TRX"
                    )

                else:

                    winner_text = (
                        f"🏆 برنده: "
                        f"{display_name(winner)}\n"
                        f"💰 برد: {money(amount)} TRX"
                    )

            else:

                # مساوی:
                # شرط هر بازیکن برگردانده می‌شود.
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
                    f"💰 شرط هر نفر "
                    f"{money(amount)} TRX برگشت داده شد."
                )

            con.commit()

        except Exception:

            con.rollback()

            log.exception(
                "finish error"
            )

            return

    # -----------------------------------------------------
    # متن نتیجه
    # -----------------------------------------------------

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
        f"📊 امتیاز:\n"
        f"👤 {creator_name}: "
        f"{creator_score}\n"
        f"👤 {opponent_name}: "
        f"{opponent_score}\n\n"
        f"{winner_text}"
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text
    )


# =========================================================
# DICE / GAME HANDLER
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

    # -----------------------------------------------------
    # پیدا کردن بازی دقیق
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

    # =====================================================
    # ROBOT MODE
    # =====================================================

    if game["mode"] == "robot":

        # فقط سازنده اجازه پرتاب دارد
        if user.id != game["creator"]:
            return

        # -------------------------------------------------
        # ضد پرتاب اضافه
        # -------------------------------------------------

        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده؛ "
                "ربات در حال بازی است."
            )

            return

        # -------------------------------------------------
        # ثبت پرتاب کاربر
        # -------------------------------------------------

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

                # اگر پرتاب کاربر تمام شده
                if (
                    current["creator_round"]
                    >= current["rounds"]
                ):

                    con.rollback()

                    await msg.reply_text(
                        "⏳ پرتاب‌های شما تمام شده."
                    )

                    return

                # ضد پرتاب اضافه
                con.execute("""
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

                if con.total_changes != 1:

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
            f"📊 پرتاب‌های شما: "
            f"{current['creator_round']}/"
            f"{current['rounds']}"
        )

        # -------------------------------------------------
        # تا وقتی کاربر تمام نکرده، ربات نمی‌اندازد
        # -------------------------------------------------

        if (
            current["creator_round"]
            <
            current["rounds"]
        ):

            await msg.reply_text(
                f"⏳ هنوز نوبت شماست.\n"
                f"پرتاب بعدی را بفرست."
            )

            return

        # -------------------------------------------------
        # تمام پرتاب‌های کاربر انجام شد
        # حالا ربات همه را می‌اندازد
        # -------------------------------------------------

        await msg.reply_text(
            "🤖 پرتاب‌های شما تمام شد.\n"
            "🤖 حالا نوبت ربات است..."
        )

        # ربات تک‌تک پرتاب می‌کند
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
                f"🤖 امتیاز ربات: "
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

    # -----------------------------------------------------
    # بازیکن اول
    # -----------------------------------------------------

    if user.id == game["creator"]:

        if game["creator_round"] >= game["rounds"]:

            await msg.reply_text(
                "⏳ پرتاب‌های شما تمام شده."
            )

            return

        # نوبت بازیکن اول فقط وقتی است که
        # تعداد پرتاب‌های او با بازیکن دوم برابر باشد.
        if (
            game["creator_round"]
            >
            game["opponent_round"]
        ):

            await msg.reply_text(
                "⏳ هنوز نوبت بازیکن دوم است."
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

                con.execute("""
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

                if con.total_changes != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend creator throw error"
                )

                return

        await msg.reply_text(
            f"👤 {display_name(game['creator'])}: "
            f"{value}\n"
            "⏳ نوبت حریف."
        )

    # -----------------------------------------------------
    # بازیکن دوم
    # -----------------------------------------------------

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

                con.execute("""
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

                if con.total_changes != 1:

                    con.rollback()
                    return

                con.commit()

            except Exception:

                con.rollback()

                log.exception(
                    "friend opponent throw error"
                )

                return

        await msg.reply_text(
            f"👤 {display_name(game['opponent'])}: "
            f"{value}"
        )

    else:

        return

    # -----------------------------------------------------
    # بررسی پایان
    # -----------------------------------------------------

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
        f"💰 موجودی {q.from_user.full_name}:\n\n"
        f"{money(balance(q.from_user.id))} TRX"
    )


# =========================================================
# BALANCE TEXT
# =========================================================

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
        f"💰 موجودی {user.full_name}:\n\n"
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

    # ضد دستور
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

    if amount > 1000000:

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

    # -----------------------------------------------------
    # انتقال اتمیک
    # -----------------------------------------------------

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
                user.id,
            )).fetchone()

            receiver_row = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
            """, (
                receiver.id,
            )).fetchone()

            if not sender_row or not receiver_row:

                con.rollback()

                await msg.reply_text(
                    "❌ حساب پیدا نشد."
                )

                return

            sender_balance = float(
                sender_row["balance"]
            )

            # ضد موجودی
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

            # کسر اتمیک
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

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

            # اضافه به گیرنده
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

async def withdraw(
    update,
    context
):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "برداشت بلاکچینی در این نسخه فعال نیست."
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
        "🎮 ساخت بازی:\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"
        "💰 موجودی در گپ:\n"
        "موجودی\n\n"
        "🔄 انتقال در گپ:\n"
        "روی پیام گیرنده Reply کن:\n"
        "انتقال 0.1\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر تمام پرتاب‌های خودش "
        "را انجام می‌دهد، سپس ربات."
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
        "👑 پنل مالک",
        reply_markup=admin_keyboard()
    )


# =========================================================
# ADMIN ADD
# =========================================================

async def admin_add(
    update,
    context
):

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

async def admin_remove(
    update,
    context
):

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

async def admin_balance(
    update,
    context
):

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
        SELECT COALESCE(SUM(balance), 0) AS b
        FROM users
        """).fetchone()["b"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 بازی‌ها: {games_count}\n"
        f"⏳ بازی فعال: {active}\n"
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

    if not update.message:
        return

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
    # موجودی کاربر
    # -----------------------------------------------------

    if action == "balance":

        if not re.fullmatch(
            r"\d+",
            text
        ):

            await update.message.reply_text(
                "❌ آیدی اشتباه است."
            )

            return

        target = int(text)

        await update.message.reply_text(
            "💰 موجودی کاربر:\n\n"
            f"{money(balance(target))} TRX"
        )

        context.user_data.pop(
            "admin",
            None
        )

        return

    # -----------------------------------------------------
    # افزایش / کاهش
    # -----------------------------------------------------

    m = re.fullmatch(
        r"^(\d+)\s+(\d{1,8}(?:\.\d{1,8})?)$",
        text
    )

    if not m:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ"
        )

        return

    target = int(
        m.group(1)
    )

    amount = float(
        m.group(2)
    )

    if amount <= 0:

        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    # اطمینان از وجود کاربر
    if not get_user(target):

        await update.message.reply_text(
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

        await update.message.reply_text(
            "✅ انجام شد.\n\n"
            f"💰 موجودی جدید:\n"
            f"{money(balance(target))} TRX"
        )

    else:

        await update.message.reply_text(
            "❌ عملیات انجام نشد؛ "
            "احتمالاً موجودی کافی نیست."
        )

    context.user_data.pop(
        "admin",
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

    # -----------------------------------------------------
    # Membership
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # Main
    # -----------------------------------------------------

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

        await q.answer()

        await q.message.reply_text(
            "🏠 منوی اصلی",
            reply_markup=keyboard(
                q.from_user.id
            )
        )

    # -----------------------------------------------------
    # Game callbacks
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # ضد دستور:
    # پیام خیلی طولانی را پردازش نکن
    # -----------------------------------------------------

    if len(text) > 200:
        return

    # -----------------------------------------------------
    # موجودی در گپ
    # -----------------------------------------------------

    if text.lower() in (
        "موجودی",
        "بالانس",
        "balance"
    ):

        await balance_text(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # پنل مالک
    # -----------------------------------------------------

    if (
        user.id == OWNER_ID
        and
        context.user_data.get("admin")
    ):

        await admin_text(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # انتقال
    # -----------------------------------------------------

    if text.startswith("انتقال"):

        await transfer_handler(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # بازی
    # -----------------------------------------------------

    parsed = parse_game(
        text
    )

    if parsed:

        await create_game(
            update,
            context
        )

        return

    # -----------------------------------------------------
    # دستورات جعلی/نامعتبر بازی
    # نادیده گرفته می‌شوند
    # -----------------------------------------------------

    # هیچ دستور دیگری اجرا نمی‌شود.


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
    # CALLBACK
    # -----------------------------------------------------

    app.add_handler(
        CallbackQueryHandler(
            callback
        )
    )

    # -----------------------------------------------------
    # بازی‌های تلگرام
    # -----------------------------------------------------

    app.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # -----------------------------------------------------
    # متن
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

    # -----------------------------------------------------
    # POLLING
    # -----------------------------------------------------

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
