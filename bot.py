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


# ============================================================
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL_USERNAME = "@BET_BT1"
CHANNEL_URL = "https://t.me/BET_BT1"

DATABASE = "bet_bot.sqlite3"

# از هر 0.1 TRX:
# برنده 0.185 می‌گیرد
# 0.015 سهم سیستم/مالک است
PAYOUT_RATE = 1.85


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DATABASE,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_database():
    with closing(db()) as conn:

        conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            name TEXT DEFAULT '',
            username TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id TEXT PRIMARY KEY,

            chat_id INTEGER NOT NULL,

            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,

            game_type TEXT NOT NULL,
            emoji TEXT NOT NULL,

            rounds INTEGER NOT NULL,
            stake REAL NOT NULL,

            creator_throws INTEGER NOT NULL DEFAULT 0,
            opponent_throws INTEGER NOT NULL DEFAULT 0,

            creator_score INTEGER NOT NULL DEFAULT 0,
            opponent_score INTEGER NOT NULL DEFAULT 0,

            mode TEXT NOT NULL DEFAULT 'friend',

            status TEXT NOT NULL DEFAULT 'waiting',

            creator_paid INTEGER NOT NULL DEFAULT 1,
            opponent_paid INTEGER NOT NULL DEFAULT 0,

            settled INTEGER NOT NULL DEFAULT 0,

            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_status
        ON games(status)
        """)

        conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_users
        ON games(creator_id, opponent_id)
        """)

        conn.commit()


# ============================================================
# USERS
# ============================================================

def ensure_user(user):
    with closing(db()) as conn:

        conn.execute("""
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
            user.username or "",
        ))

        conn.commit()


def user_exists(user_id):
    with closing(db()) as conn:

        row = conn.execute("""
        SELECT user_id
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        return row is not None


def get_balance(user_id):
    with closing(db()) as conn:

        row = conn.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            return 0.0

        return float(row["balance"])


def change_balance(user_id, amount):
    """
    تغییر موجودی به صورت اتمیک.
    """

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            row = conn.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            if not row:
                conn.rollback()
                return False

            old_balance = float(row["balance"])
            new_balance = round(
                old_balance + float(amount),
                8,
            )

            if new_balance < 0:
                conn.rollback()
                return False

            conn.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                new_balance,
                user_id,
            ))

            conn.commit()

            return True

        except Exception:
            conn.rollback()
            logger.exception("Balance transaction failed")
            return False


# ============================================================
# FORMATTERS
# ============================================================

def normalize_digits(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
    )

    return str(text).translate(table)


def format_trx(value):
    value = round(float(value), 8)

    return (
        f"{value:.8f}"
        .rstrip("0")
        .rstrip(".")
        or "0"
    )


# ============================================================
# GAME TYPES
# ============================================================

GAME_TYPES = {
    "تاس": "🎲",
    "دارت": "🎯",
    "بسکتبال": "🏀",
    "بولینگ": "🎳",
}


def parse_game(text):
    """
    نمونه:

    4 تاس 0.1
    4 تاس ۰.۱
    20 دارت 1
    100 بسکتبال 0.1
    999 بولینگ 0.1
    """

    text = normalize_digits(
        text.strip()
    )

    pattern = re.compile(
        r"^(\d+)\s+"
        r"(تاس|دارت|بسکتبال|بولینگ)\s+"
        r"(\d+(?:\.\d+)?)$"
    )

    match = pattern.match(text)

    if not match:
        return None

    rounds = int(match.group(1))
    game_name = match.group(2)
    stake = float(match.group(3))

    if rounds <= 0:
        return None

    if stake <= 0:
        return None

    if stake > 1000000:
        return None

    return {
        "rounds": rounds,
        "name": game_name,
        "emoji": GAME_TYPES[game_name],
        "stake": round(stake, 8),
    }


# ============================================================
# GAME DATABASE
# ============================================================

def get_game(game_id):
    with closing(db()) as conn:

        return conn.execute("""
        SELECT *
        FROM games
        WHERE game_id=?
        """, (
            game_id,
        )).fetchone()


def user_has_active_game(user_id):
    with closing(db()) as conn:

        row = conn.execute("""
        SELECT game_id
        FROM games
        WHERE settled=0
        AND status IN ('waiting', 'playing')
        AND (
            creator_id=?
            OR opponent_id=?
        )
        LIMIT 1
        """, (
            user_id,
            user_id,
        )).fetchone()

        return row


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(bot, user_id):

    try:

        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id,
        )

        return member.status in (
            "creator",
            "administrator",
            "member",
        )

    except Exception:

        # اگر بات دسترسی بررسی عضویت نداشت،
        # ربات را کاملاً از کار نمی‌اندازیم.
        logger.warning(
            "Could not check channel membership."
        )

        return True


async def require_channel(update, context):

    user = update.effective_user

    if not user:
        return False

    if user.id == OWNER_ID:
        return True

    ok = await check_membership(
        context.bot,
        user.id,
    )

    if ok:
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 ورود به کانال",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_membership",
            )
        ],
    ])

    if update.callback_query:

        await update.callback_query.answer(
            "❌ ابتدا عضو کانال شوید.",
            show_alert=True,
        )

        await update.callback_query.message.reply_text(
            "❌ برای استفاده از ربات ابتدا عضو کانال شوید:",
            reply_markup=keyboard,
        )

    elif update.message:

        await update.message.reply_text(
            "❌ برای استفاده از ربات ابتدا عضو کانال شوید:",
            reply_markup=keyboard,
        )

    return False


# ============================================================
# MAIN KEYBOARD
# ============================================================

def main_keyboard(user_id):

    rows = [
        [
            InlineKeyboardButton(
                "🎮 بازی",
                callback_data="games",
            ),
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer_help",
            ),
            InlineKeyboardButton(
                "💸 برداشت",
                callback_data="withdraw",
            ),
        ],
        [
            InlineKeyboardButton(
                "📖 راهنما",
                callback_data="help",
            ),
            InlineKeyboardButton(
                "🎯 مثال بازی",
                callback_data="examples",
            ),
        ],
    ]

    if user_id == OWNER_ID:

        rows.append([
            InlineKeyboardButton(
                "👑 پنل مالک",
                callback_data="admin",
            )
        ])

    return InlineKeyboardMarkup(rows)


# ============================================================
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    user = update.effective_user

    ensure_user(user)

    if not await require_channel(
        update,
        context,
    ):
        return

    await update.message.reply_text(
        "🎮 BET_BT\n\n"
        "خوش آمدی 👋\n\n"
        "از دکمه «🎮 بازی» برای شروع استفاده کن.\n\n"
        "💰 واحد حساب: TRX",
        reply_markup=main_keyboard(user.id),
    )


# ============================================================
# GAMES MENU
# ============================================================

async def games_menu(update, context):

    q = update.callback_query

    await q.answer()

    if not await require_channel(
        update,
        context,
    ):
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data="friend_info",
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data="robot_info",
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 مثال",
                callback_data="examples",
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="home",
            )
        ],
    ])

    await q.message.reply_text(
        "🎮 انتخاب نوع بازی\n\n"
        "ابتدا نوع بازی را انتخاب کن.\n\n"
        "فرمت مبلغ و تعداد برای همه بازی‌ها یکی است.",
        reply_markup=keyboard,
    )


# ============================================================
# EXAMPLES
# ============================================================

async def examples(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🎮 نمونه‌های صحیح:\n\n"

        "🎲 4 تاس 0.1\n"
        "🎲 4 تاس ۰.۱\n"
        "🎲 10 تاس 0.1\n\n"

        "🎯 4 دارت 0.1\n"
        "🎯 10 دارت 0.1\n\n"

        "🏀 4 بسکتبال 0.1\n"
        "🏀 10 بسکتبال 0.1\n\n"

        "🎳 4 بولینگ 0.1\n"
        "🎳 10 بولینگ 0.1\n\n"

        "🔢 تعداد پرتاب محدود نیست.\n"
        "💰 مبلغ برای همه بازی‌ها یکسان است."
    )


# ============================================================
# FRIEND INFO
# ============================================================

async def friend_info(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "👥 بازی با دوستان\n\n"

        "مثال:\n"
        "4 تاس 0.1\n\n"

        "بعد از ساخت بازی، دکمه ورود برای دیگران نمایش داده می‌شود.\n\n"

        "⚠️ در این حالت ربات هیچ 🎲🎯🏀🎳 پرتابی انجام نمی‌دهد.\n"
        "فقط دو کاربر پرتاب می‌کنند."
    )


# ============================================================
# ROBOT INFO
# ============================================================

async def robot_info(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🤖 بازی با ربات\n\n"

        "مثال:\n"
        "4 تاس 0.1\n\n"

        "بعد از شروع:\n"
        "1️⃣ کاربر ایموجی بازی را می‌فرستد.\n"
        "2️⃣ ربات پرتاب خودش را انجام می‌دهد.\n"
        "3️⃣ این روند تا تعداد تعیین‌شده ادامه دارد.\n\n"

        "⚠️ ربات قبل از پرتاب کاربر شروع نمی‌کند."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context):

    if not update.message:
        return

    if update.message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    parsed = parse_game(
        update.message.text or ""
    )

    if not parsed:
        return

    user = update.effective_user

    ensure_user(user)

    if not await require_channel(
        update,
        context,
    ):
        return

    active = user_has_active_game(
        user.id
    )

    if active:

        await update.message.reply_text(
            "❌ شما در حال بازی است.\n\n"
            "اول بازی قبلی را تمام یا لغو کن."
        )

        return

    stake = parsed["stake"]

    if get_balance(user.id) < stake:

        await update.message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {format_trx(get_balance(user.id))} TRX\n"
            f"💵 مبلغ ورود: {format_trx(stake)} TRX"
        )

        return

    # قفل کردن مبلغ
    if not change_balance(
        user.id,
        -stake,
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    game_id = secrets.token_hex(12)

    with closing(db()) as conn:

        try:

            conn.execute("""
            INSERT INTO games (
                game_id,
                chat_id,
                creator_id,
                game_type,
                emoji,
                rounds,
                stake,
                mode,
                status,
                creator_paid
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'friend', 'waiting', 1)
            """, (
                game_id,
                update.message.chat.id,
                user.id,
                parsed["name"],
                parsed["emoji"],
                parsed["rounds"],
                stake,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_balance(
                user.id,
                stake,
            )

            logger.exception(
                "Could not create game"
            )

            await update.message.reply_text(
                "❌ خطا در ساخت بازی."
            )

            return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_friend:{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"join_robot:{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel:{game_id}",
            )
        ],
    ])

    await update.message.reply_text(
        f"{parsed['emoji']} بازی ساخته شد.\n\n"

        f"🎮 بازی: {parsed['name']}\n"
        f"🔢 تعداد: {parsed['rounds']}\n"
        f"💰 ورود: {format_trx(stake)} TRX\n\n"

        "حالت بازی را انتخاب کن:",
        reply_markup=keyboard,
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    user = q.from_user

    ensure_user(user)

    if not await require_channel(
        update,
        context,
    ):
        return

    if user.id == game["creator_id"]:

        await q.answer(
            "❌ خودت سازنده بازی هستی.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True,
        )

        return

    active = user_has_active_game(
        user.id
    )

    if active:

        await q.answer(
            "❌ شما در حال بازی است.",
            show_alert=True,
        )

        return

    stake = float(game["stake"])

    if get_balance(user.id) < stake:

        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    if not change_balance(
        user.id,
        -stake,
    ):

        await q.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
            AND status='waiting'
            AND settled=0
            """, (
                game_id,
            )).fetchone()

            if not current:

                conn.rollback()

                change_balance(
                    user.id,
                    stake,
                )

                await q.answer(
                    "❌ شخص دیگری زودتر وارد شد.",
                    show_alert=True,
                )

                return

            conn.execute("""
            UPDATE games
            SET
                opponent_id=?,
                opponent_paid=1,
                mode='friend',
                status='playing'
            WHERE game_id=?
            """, (
                user.id,
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            change_balance(
                user.id,
                stake,
            )

            await q.answer(
                "❌ خطا.",
                show_alert=True,
            )

            return

    await q.answer(
        "✅ وارد بازی شدی."
    )

    await q.message.reply_text(
        f"{game['emoji']} بازی با دوستان شروع شد.\n\n"

        f"🎮 {game['game_type']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n\n"

        "👤 بازیکن اول پرتاب می‌کند.\n"
        "👤 سپس بازیکن دوم پرتاب می‌کند.\n\n"

        "⚠️ ربات هیچ پرتابی انجام نمی‌دهد."
    )


# ============================================================
# JOIN ROBOT
# ============================================================

async def join_robot(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    user = q.from_user

    if user.id != game["creator_id"]:

        await q.answer(
            "❌ فقط سازنده بازی می‌تواند این گزینه را بزند.",
            show_alert=True,
        )

        return

    if game["status"] != "waiting":

        await q.answer(
            "❌ بازی شروع شده.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        conn.execute("""
        UPDATE games
        SET
            opponent_id=0,
            mode='robot',
            status='playing'
        WHERE game_id=?
        AND status='waiting'
        AND settled=0
        """, (
            game_id,
        ))

        conn.commit()

    await q.answer()

    await q.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"

        f"{game['emoji']} {game['game_type']}\n"
        f"🔢 تعداد پرتاب: {game['rounds']}\n\n"

        f"👤 حالا خودت {game['emoji']} را بفرست.\n"
        "🤖 بعد از پرتاب تو، ربات پرتاب می‌کند."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    q = update.callback_query

    game_id = q.data.split(":", 1)[1]

    game = get_game(game_id)

    if not game:

        await q.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True,
        )

        return

    if q.from_user.id != game["creator_id"]:

        await q.answer(
            "❌ فقط سازنده می‌تواند بازی را لغو کند.",
            show_alert=True,
        )

        return

    if game["settled"]:

        await q.answer(
            "❌ این بازی قبلاً بسته شده.",
            show_alert=True,
        )

        return

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
            SELECT *
            FROM games
            WHERE game_id=?
            AND settled=0
            """, (
                game_id,
            )).fetchone()

            if not current:

                conn.rollback()

                await q.answer(
                    "❌ بازی بسته شده.",
                    show_alert=True,
                )

                return

            conn.execute("""
            UPDATE games
            SET
                status='cancelled',
                settled=1
            WHERE game_id=?
            """, (
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            await q.answer(
                "❌ خطا.",
                show_alert=True,
            )

            return

    # برگشت مبلغ نفر اول
    change_balance(
        game["creator_id"],
        float(game["stake"]),
    )

    # اگر نفر دوم پول داده، برگشت
    if game["opponent_id"] not in (
        None,
        0,
    ):

        change_balance(
            game["opponent_id"],
            float(game["stake"]),
        )

    await q.answer(
        "❌ بازی لغو شد."
    )

    await q.message.reply_text(
        "❌ بازی لغو شد.\n\n"
        "💰 مبلغ بازیکنان برگشت داده شد."
    )


# ============================================================
# DICE / DART / BASKETBALL / BOWLING
# ============================================================

async def game_dice_handler(update, context):

    message = update.message

    if not message:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    dice = message.dice

    if not dice:
        return

    emoji = dice.emoji

    if emoji not in (
        "🎲",
        "🎯",
        "🏀",
        "🎳",
    ):
        return

    user = update.effective_user

    # --------------------------------------------------------
    # پیدا کردن بازی فعال همین کاربر و همین ایموجی
    # --------------------------------------------------------

    with closing(db()) as conn:

        game = conn.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND status='playing'
        AND settled=0
        AND emoji=?
        AND (
            creator_id=?
            OR opponent_id=?
        )
        ORDER BY created_at DESC
        LIMIT 1
        """, (
            message.chat.id,
            emoji,
            user.id,
            user.id,
        )).fetchone()

    if not game:
        return

    value = int(dice.value)

    # ========================================================
    # ROBOT MODE
    # ========================================================

    if game["mode"] == "robot":

        # فقط کاربر اصلی
        if user.id != game["creator_id"]:
            return

        # فقط وقتی نوبت کاربر است
        if (
            game["creator_throws"]
            >= game["rounds"]
        ):

            return

        if (
            game["creator_throws"]
            > game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ اول نوبت ربات است."
            )

            return

        # ثبت پرتاب کاربر
        with closing(db()) as conn:

            conn.execute("""
            UPDATE games
            SET
                creator_throws=creator_throws+1,
                creator_score=creator_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 پرتاب شما: {value}\n"
            "🤖 نوبت ربات..."
        )

        # ----------------------------------------------------
        # این تنها جایی است که ربات پرتاب می‌کند.
        # ----------------------------------------------------

        try:

            robot_message = await context.bot.send_dice(
                chat_id=message.chat.id,
                emoji=emoji,
            )

            robot_value = int(
                robot_message.dice.value
            )

        except Exception as exc:

            logger.exception(
                "Robot dice failed"
            )

            await message.reply_text(
                "❌ پرتاب ربات انجام نشد."
            )

            return

        with closing(db()) as conn:

            conn.execute("""
            UPDATE games
            SET
                opponent_throws=opponent_throws+1,
                opponent_score=opponent_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                robot_value,
                game["game_id"],
            ))

            conn.commit()

        await message.reply_text(
            f"🤖 پرتاب ربات: {robot_value}"
        )

        current = get_game(
            game["game_id"]
        )

        if not current:
            return

        if (
            current["creator_throws"]
            >= current["rounds"]
            and
            current["opponent_throws"]
            >= current["rounds"]
        ):

            await finish_game(
                game["game_id"],
                context,
            )

        return

    # ========================================================
    # FRIEND MODE
    # ========================================================

    if game["mode"] != "friend":
        return

    # --------------------------------------------------------
    # بازیکن اول
    # --------------------------------------------------------

    if user.id == game["creator_id"]:

        if (
            game["creator_throws"]
            >= game["rounds"]
        ):
            return

        # اگر نفر اول قبلاً این راند را زده
        if (
            game["creator_throws"]
            >
            game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ در انتظار بازیکن دوم..."
            )

            return

        with closing(db()) as conn:

            conn.execute("""
            UPDATE games
            SET
                creator_throws=creator_throws+1,
                creator_score=creator_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 بازیکن اول: {value}\n"
            "⏳ نوبت بازیکن دوم است."
        )

    # --------------------------------------------------------
    # بازیکن دوم
    # --------------------------------------------------------

    elif user.id == game["opponent_id"]:

        if (
            game["opponent_throws"]
            >= game["rounds"]
        ):
            return

        # نفر اول باید اول بزند
        if (
            game["creator_throws"]
            <= game["opponent_throws"]
        ):

            await message.reply_text(
                "⏳ هنوز نوبت شما نیست."
            )

            return

        with closing(db()) as conn:

            conn.execute("""
            UPDATE games
            SET
                opponent_throws=opponent_throws+1,
                opponent_score=opponent_score+?
            WHERE game_id=?
            AND settled=0
            """, (
                value,
                game["game_id"],
            ))

            conn.commit()

        await message.reply_text(
            f"👤 بازیکن دوم: {value}"
        )

    else:
        return

    # --------------------------------------------------------
    # بررسی پایان
    # --------------------------------------------------------

    current = get_game(
        game["game_id"]
    )

    if not current:
        return

    if (
        current["creator_throws"]
        >= current["rounds"]
        and
        current["opponent_throws"]
        >= current["rounds"]
    ):

        await finish_game(
            game["game_id"],
            context,
        )


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(game_id, context):

    game = get_game(game_id)

    if not game:
        return

    if game["settled"]:
        return

    creator_score = int(
        game["creator_score"]
    )

    opponent_score = int(
        game["opponent_score"]
    )

    # --------------------------------------------------------
    # قفل تسویه
    # --------------------------------------------------------

    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            current = conn.execute("""
            SELECT settled
            FROM games
            WHERE game_id=?
            """, (
                game_id,
            )).fetchone()

            if not current:
                conn.rollback()
                return

            if current["settled"]:
                conn.rollback()
                return

            conn.execute("""
            UPDATE games
            SET
                settled=1,
                status='finished'
            WHERE game_id=?
            """, (
                game_id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            logger.exception(
                "Game settlement failed"
            )

            return

    stake = float(game["stake"])

    # ========================================================
    # مساوی
    # ========================================================

    if creator_score == opponent_score:

        change_balance(
            game["creator_id"],
            stake,
        )

        if game["mode"] == "friend":

            change_balance(
                game["opponent_id"],
                stake,
            )

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"

                "🤝 بازی مساوی شد.\n"
                "💰 مبلغ ورود برگشت داده شد."
            )

        else:

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"

                "🤝 مساوی شد.\n"
                "💰 مبلغ ورود برگشت داده شد."
            )

    # ========================================================
    # بازیکن اول برنده
    # ========================================================

    elif creator_score > opponent_score:

        reward = round(
            stake * PAYOUT_RATE,
            8,
        )

        change_balance(
            game["creator_id"],
            reward,
        )

        if game["mode"] == "friend":

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"

                "🏆 بازیکن اول برنده شد.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

        else:

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"

                "🏆 شما برنده شدید.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

    # ========================================================
    # بازیکن دوم / ربات برنده
    # ========================================================

    else:

        reward = round(
            stake * PAYOUT_RATE,
            8,
        )

        if game["mode"] == "friend":

            change_balance(
                game["opponent_id"],
                reward,
            )

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 بازیکن اول: {creator_score}\n"
                f"👤 بازیکن دوم: {opponent_score}\n\n"

                "🏆 بازیکن دوم برنده شد.\n"
                f"💰 جایزه: {format_trx(reward)} TRX"
            )

        else:

            text = (
                f"{game['emoji']} نتیجه بازی\n\n"

                f"👤 شما: {creator_score}\n"
                f"🤖 ربات: {opponent_score}\n\n"

                "🤖 ربات برنده شد."
            )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=text,
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    q = update.callback_query

    await q.answer()

    user = q.from_user

    ensure_user(user)

    await q.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"{format_trx(get_balance(user.id))} TRX"
    )


async def group_balance(update, context):

    if not update.message:
        return

    if update.message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    text = normalize_digits(
        update.message.text.strip()
    )

    if text != "موجودی":
        return

    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        f"💰 موجودی شما: "
        f"{format_trx(get_balance(user.id))} TRX"
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_help(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🔄 انتقال موجودی\n\n"
        "روی پیام کاربر Reply کن و بنویس:\n\n"
        "انتقال 1\n\n"
        "مثلاً:\n"
        "انتقال 0.1"
    )


async def transfer_command(update, context):

    if not update.message:
        return

    text = normalize_digits(
        update.message.text.strip()
    )

    if not text.startswith("انتقال"):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ باید روی پیام گیرنده Reply کنی."
        )
        return

    parts = text.split()

    if len(parts) != 2:
        await update.message.reply_text(
            "❌ نمونه صحیح:\n"
            "انتقال 0.1"
        )
        return

    try:
        amount = float(parts[1])
    except ValueError:
        await update.message.reply_text(
            "❌ مبلغ صحیح نیست."
        )
        return

    if amount <= 0:
        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    sender = update.effective_user

    receiver = (
        update.message
        .reply_to_message
        .from_user
    )

    if not receiver:
        return

    if receiver.is_bot:
        await update.message.reply_text(
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )
        return

    if sender.id == receiver.id:
        await update.message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return

    ensure_user(sender)
    ensure_user(receiver)

    # تراکنش اتمیک
    with closing(db()) as conn:

        try:

            conn.execute("BEGIN IMMEDIATE")

            sender_row = conn.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (
                sender.id,
            )).fetchone()

            if not sender_row:

                conn.rollback()

                await update.message.reply_text(
                    "❌ کاربر پیدا نشد."
                )

                return

            sender_balance = float(
                sender_row["balance"]
            )

            if sender_balance < amount:

                conn.rollback()

                await update.message.reply_text(
                    "❌ موجودی کافی نیست."
                )

                return

            conn.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                sender.id,
            ))

            conn.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver.id,
            ))

            conn.commit()

        except Exception:

            conn.rollback()

            logger.exception(
                "Transfer failed"
            )

            await update.message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await update.message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {receiver.full_name}\n"
        f"💰 مقدار: {format_trx(amount)} TRX"
    )


# ============================================================
# WITHDRAW
# ============================================================

async def withdraw(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "💸 برداشت\n\n"
        "سیستم برداشت در این نسخه فعال نشده است."
    )


# ============================================================
# HELP
# ============================================================

async def help_menu(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "📖 راهنما\n\n"

        "🎮 برای بازی از دکمه «بازی» استفاده کن.\n\n"

        "نمونه:\n"
        "4 تاس 0.1\n"
        "4 دارت 0.1\n"
        "4 بسکتبال 0.1\n"
        "4 بولینگ 0.1\n\n"

        "💰 موجودی:\n"
        "موجودی\n\n"

        "🔄 انتقال:\n"
        "روی پیام کاربر Reply کن:\n"
        "انتقال 0.1\n\n"

        "🎮 بازی با دوستان:\n"
        "فقط دو کاربر پرتاب می‌کنند.\n\n"

        "🤖 بازی با ربات:\n"
        "کاربر پرتاب می‌کند، سپس ربات."
    )


# ============================================================
# HOME
# ============================================================

async def home(update, context):

    q = update.callback_query

    await q.answer()

    await q.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_keyboard(
            q.from_user.id
        ),
    )


# ============================================================
# MEMBERSHIP BUTTON
# ============================================================

async def check_membership_button(update, context):

    q = update.callback_query

    ok = await check_membership(
        context.bot,
        q.from_user.id,
    )

    if ok:

        await q.answer(
            "✅ عضویت تأیید شد.",
            show_alert=True,
        )

        await q.message.reply_text(
            "✅ حالا می‌توانی از ربات استفاده کنی.",
            reply_markup=main_keyboard(
                q.from_user.id
            ),
        )

    else:

        await q.answer(
            "❌ هنوز عضو کانال نیستی.",
            show_alert=True,
        )


# ============================================================
# ADMIN MENU
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add",
            ),
            InlineKeyboardButton(
                "➖ کاهش موجودی",
                callback_data="admin_remove",
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 موجودی کاربر",
                callback_data="admin_balance",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔙 برگشت",
                callback_data="home",
            )
        ],
    ])


async def admin_menu(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:

        await q.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )

        return

    await q.answer()

    await q.message.reply_text(
        "👑 پنل مالک",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN ADD
# ============================================================

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


# ============================================================
# ADMIN REMOVE
# ============================================================

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
        "123456789 10"
    )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def admin_balance(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    context.user_data["admin_action"] = "balance"

    await q.message.reply_text(
        "💰 آیدی عددی کاربر را بفرست."
    )


# ============================================================
# ADMIN STATS
# ============================================================

async def admin_stats(update, context):

    q = update.callback_query

    if q.from_user.id != OWNER_ID:
        return

    await q.answer()

    with closing(db()) as conn:

        users = conn.execute("""
        SELECT COUNT(*) AS c
        FROM users
        """).fetchone()["c"]

        games = conn.execute("""
        SELECT COUNT(*) AS c
        FROM games
        """).fetchone()["c"]

        active = conn.execute("""
        SELECT COUNT(*) AS c
        FROM games
        WHERE settled=0
        """).fetchone()["c"]

        total_balance = conn.execute("""
        SELECT COALESCE(SUM(balance),0) AS s
        FROM users
        """).fetchone()["s"]

    await q.message.reply_text(
        "📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"🎮 کل بازی‌ها: {games}\n"
        f"⏳ بازی‌های فعال: {active}\n"
        f"💰 مجموع موجودی: "
        f"{format_trx(total_balance)} TRX"
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def admin_text_handler(update, context):

    if not update.message:
        return

    user = update.effective_user

    if user.id != OWNER_ID:
        return

    action = context.user_data.get(
        "admin_action"
    )

    if not action:
        return

    text = normalize_digits(
        update.message.text.strip()
    )

    # --------------------------------------------------------
    # موجودی کاربر
    # --------------------------------------------------------

    if action == "balance":

        try:
            target_id = int(text)
        except ValueError:

            await update.message.reply_text(
                "❌ آیدی صحیح نیست."
            )

            return

        if not user_exists(target_id):

            await update.message.reply_text(
                "❌ کاربر پیدا نشد."
            )

            return

        await update.message.reply_text(
            f"🆔 {target_id}\n"
            f"💰 موجودی: "
            f"{format_trx(get_balance(target_id))} TRX"
        )

        context.user_data.pop(
            "admin_action",
            None,
        )

        return

    # --------------------------------------------------------
    # افزایش / کاهش
    # --------------------------------------------------------

    parts = text.split()

    if len(parts) != 2:

        await update.message.reply_text(
            "❌ فرمت:\n"
            "آیدی مبلغ"
        )

        return

    try:

        target_id = int(parts[0])
        amount = float(parts[1])

    except ValueError:

        await update.message.reply_text(
            "❌ مقدار صحیح نیست."
        )

        return

    if amount <= 0:

        await update.message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )

        return

    if not user_exists(target_id):

        await update.message.reply_text(
            "❌ این کاربر هنوز ربات را استارت نکرده است."
        )

        return

    if action == "add":

        ok = change_balance(
            target_id,
            amount,
        )

        title = "افزایش"

    else:

        ok = change_balance(
            target_id,
            -amount,
        )

        title = "کاهش"

    if not ok:

        await update.message.reply_text(
            "❌ عملیات انجام نشد.\n"
            "ممکن است موجودی برای کاهش کافی نباشد."
        )

        return

    await update.message.reply_text(
        f"✅ {title} موجودی انجام شد.\n\n"
        f"🆔 {target_id}\n"
        f"💰 مقدار: {format_trx(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{format_trx(get_balance(target_id))} TRX"
    )

    context.user_data.pop(
        "admin_action",
        None,
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):

    q = update.callback_query

    data = q.data

    if data == "check_membership":
        await check_membership_button(
            update,
            context,
        )

    elif data == "home":
        await home(
            update,
            context,
        )

    elif data == "games":
        await games_menu(
            update,
            context,
        )

    elif data == "friend_info":
        await friend_info(
            update,
            context,
        )

    elif data == "robot_info":
        await robot_info(
            update,
            context,
        )

    elif data == "examples":
        await examples(
            update,
            context,
        )

    elif data == "balance":
        await show_balance(
            update,
            context,
        )

    elif data == "transfer_help":
        await transfer_help(
            update,
            context,
        )

    elif data == "withdraw":
        await withdraw(
            update,
            context,
        )

    elif data == "help":
        await help_menu(
            update,
            context,
        )

    elif data == "admin":
        await admin_menu(
            update,
            context,
        )

    elif data == "admin_add":
        await admin_add(
            update,
            context,
        )

    elif data == "admin_remove":
        await admin_remove(
            update,
            context,
        )

    elif data == "admin_balance":
        await admin_balance(
            update,
            context,
        )

    elif data == "admin_stats":
        await admin_stats(
            update,
            context,
        )

    elif data.startswith("join_friend:"):
        await join_friend(
            update,
            context,
        )

    elif data.startswith("join_robot:"):
        await join_robot(
            update,
            context,
        )

    elif data.startswith("cancel:"):
        await cancel_game(
            update,
            context,
        )


# ============================================================
# TEXT ROUTER
# ============================================================

async def text_router(update, context):

    if not update.message:
        return

    text = update.message.text or ""

    user = update.effective_user

    ensure_user(user)

    # --------------------------------------------------------
    # پنل مالک
    # --------------------------------------------------------

    if (
        user.id == OWNER_ID
        and context.user_data.get("admin_action")
    ):

        await admin_text_handler(
            update,
            context,
        )

        return

    normalized = normalize_digits(
        text.strip()
    )

    # --------------------------------------------------------
    # انتقال
    # --------------------------------------------------------

    if normalized.startswith("انتقال"):

        await transfer_command(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # موجودی
    # --------------------------------------------------------

    if normalized == "موجودی":

        await group_balance(
            update,
            context,
        )

        return

    # --------------------------------------------------------
    # ساخت بازی
    # --------------------------------------------------------

    if parse_game(text):

        await create_game(
            update,
            context,
        )

        return


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):

    logger.exception(
        "Unhandled exception:",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    init_database()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    application.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # دکمه‌ها
    application.add_handler(
        CallbackQueryHandler(
            callback_router,
        )
    )

    # ایموجی‌های واقعی تلگرام
    # مهم: قبل از text router قرار دارد.
    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            game_dice_handler,
        )
    )

    # تمام پیام‌های متنی
    # فقط یک Text Router داریم تا چند دستور با هم تداخل نکنند.
    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_router,
        )
    )

    application.add_error_handler(
        error_handler,
    )

    logger.info(
        "BET BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
